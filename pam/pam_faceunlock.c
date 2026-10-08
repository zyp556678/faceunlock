/*
 * pam_faceunlock.so —— faceunlock 的 PAM 认证模块
 *
 * 设计原则（安全关键，改动前请先读 README 的安全章节）:
 *
 *  1. 本模块**只做转发**，不解析配置、不碰摄像头、不做识别。
 *     真正的判定在 root 助手 /usr/libexec/faceunlock-auth 里，
 *     它把 (user, service, rhost, tty) 作为参数接收，自行决定是否适用。
 *     这样避免在 C 里写配置解析器，也避免 PAM 参数被误用为信任来源。
 *
 *  2. 身份只取 PAM_USER，绝不读 getenv("USER")/LOGNAME。
 *
 *  3. 退出码映射:
 *       0  -> PAM_SUCCESS        （人脸匹配）
 *       1  -> PAM_AUTH_ERR       （明确不匹配，PAM 栈继续，通常落到密码）
 *       其它-> PAM_IGNORE        （不适用/异常/被杀，等价于"这模块没意见"）
 *
 *  4. **硬超时**: 助手可能因摄像头驱动卡死。本模块用 alarm() 兜底，
 *     超时后 SIGKILL 掉子进程并返回 PAM_IGNORE。没有这个机制，
 *     一个卡住的摄像头就能把所有人的登录卡死 —— 那是自杀式设计。
 *
 *  5. 对 root 用户直接 PAM_IGNORE。
 *
 *  6. **fork 之前先看 /dev 有没有 video* 设备**（Ubuntu 26.04 必需）。
 *     polkit 127 把 polkit-1 的 PAM 栈放进 socket 激活的
 *     polkit-agent-helper@.service，那里 PrivateDevices=yes + DevicePolicy=strict
 *     只放行 /dev/null：沙箱里根本没有 /dev/video*，摄像头必然打不开。
 *     不预检的话，每次授权都要白等一次 fork/exec + OpenCV 导入（本机实测
 *     5~6.5 s CPU）+ 8 秒兜底超时，用户在授权框前看着"卡住"，连密码都
 *     来不及输——这正是"无法回退输入密码"的根因之一。
 *     预检命中就直接 PAM_IGNORE（毫秒级），密码提示立刻出现。
 *     用 video_probe=0 可关闭该预检（摄像头只走 libcamera/pipewire、
 *     /dev 下没有 video* 的机器需要它，但那种机器本模块的 V4L2 采集也用不了）。
 *
 * 编译: gcc -shared -fPIC -O2 -Wall -Wextra -o pam_faceunlock.so pam_faceunlock.c -lpam
 * 安装: /usr/lib/x86_64-linux-gnu/security/pam_faceunlock.so
 *
 * 推荐 PAM 用法（见 /usr/share/pam-configs/faceunlock）:
 *   auth [success=end default=ignore] pam_faceunlock.so
 * 绝不要用 required —— 摄像头坏了会把密码也一起挡掉。
 */
#define PAM_SM_AUTH
#define _GNU_SOURCE

#include <security/pam_modules.h>
#include <security/pam_appl.h>
#include <security/pam_ext.h>

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <syslog.h>
#include <unistd.h>

#define DEFAULT_HELPER "/usr/libexec/faceunlock-auth"
#define DEFAULT_TIMEOUT 8 /* 秒；助手内部自己还有更短的软超时 */

static volatile sig_atomic_t g_child = 0;

static void on_alarm(int sig)
{
    (void)sig;
    if (g_child > 0) {
        kill((pid_t)g_child, SIGKILL);
    }
}

/* 解析 "key=value" 形式的模块参数 */
static const char *opt_value(int argc, const char **argv, const char *key,
                             const char *fallback)
{
    size_t klen = strlen(key);
    for (int i = 0; i < argc; i++) {
        if (strncmp(argv[i], key, klen) == 0 && argv[i][klen] == '=') {
            return argv[i] + klen + 1;
        }
    }
    return fallback;
}

static int has_flag(int argc, const char **argv, const char *flag)
{
    for (int i = 0; i < argc; i++) {
        if (strcmp(argv[i], flag) == 0) {
            return 1;
        }
    }
    return 0;
}

/* /dev 下是否存在 V4L2 设备节点（video0、video1 …）。
 *
 * 只做 readdir，不 open 设备，所以不会被摄像头占用/驱动卡住拖住。
 * 读不了 /dev（理论上不该发生）时返回 1：查不出来就不自作主张跳过，
 * 交回给助手按它自己的判断处理（失败一律回退密码，安全语义不变）。
 */
static int has_video_device(void)
{
    DIR *d = opendir("/dev");
    if (d == NULL) {
        return 1;
    }
    int found = 0;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        if (strncmp(e->d_name, "video", 5) != 0) {
            continue;
        }
        const char *p = e->d_name + 5;
        if (*p == '\0') {
            continue;
        }
        int numeric = 1;
        for (; *p != '\0'; p++) {
            if (*p < '0' || *p > '9') {
                numeric = 0;
                break;
            }
        }
        if (numeric) {
            found = 1;
            break;
        }
    }
    closedir(d);
    return found;
}

PAM_EXTERN int pam_sm_authenticate(pam_handle_t *pamh, int flags, int argc,
                                   const char **argv)
{
    (void)flags;

    const char *user = NULL;
    const char *service = NULL;
    const char *rhost = NULL;
    const char *tty = NULL;
    int rc;

    const char *helper = opt_value(argc, argv, "helper", DEFAULT_HELPER);
    int debug = has_flag(argc, argv, "debug");
    int timeout = atoi(opt_value(argc, argv, "timeout", "0"));
    if (timeout <= 0) {
        timeout = DEFAULT_TIMEOUT;
    }
    /* /dev 预检开关（默认开；见文件头第 6 条） */
    int video_probe = strcmp(opt_value(argc, argv, "video_probe", "1"), "0") != 0;

    rc = pam_get_user(pamh, &user, NULL);
    if (rc != PAM_SUCCESS || user == NULL || user[0] == '\0') {
        return PAM_IGNORE;
    }
    /* root 永远走密码 */
    if (strcmp(user, "root") == 0) {
        return PAM_IGNORE;
    }

    pam_get_item(pamh, PAM_SERVICE, (const void **)&service);
    pam_get_item(pamh, PAM_RHOST, (const void **)&rhost);
    pam_get_item(pamh, PAM_TTY, (const void **)&tty);

    if (service == NULL) {
        service = "unknown";
    }
    if (rhost == NULL) {
        rhost = "";
    }
    if (tty == NULL) {
        tty = "";
    }

    if (access(helper, X_OK) != 0) {
        pam_syslog(pamh, LOG_WARNING,
                   "faceunlock: helper %s 不可执行(%s)，跳过", helper,
                   strerror(errno));
        return PAM_IGNORE;
    }

    if (video_probe && !has_video_device()) {
        /* 典型场景：Ubuntu 26.04 的 polkit 127 把 PAM 栈关进
         * PrivateDevices=yes + DevicePolicy=strict(+DeviceAllow=/dev/null) 的
         * 沙箱，/dev 下没有 video*。这里必须**立刻**放手：
         *   * 不 pam_info：这时候提示"请看向摄像头"是骗人的，而且会把
         *     polkit 的授权框变成一条误导信息，用户找不到密码输入框；
         *   * 不 fork：省掉 OpenCV 导入与 8 秒兜底超时，密码提示立刻出现。
         * 返回 PAM_IGNORE = "本模块没意见"，PAM 栈继续走 pam_unix 密码。
         */
        pam_syslog(pamh, LOG_NOTICE,
                   "faceunlock: user=%s service=%s /dev 下没有 video* 设备"
                   "（polkit 沙箱/摄像头未接），跳过人脸直接走密码",
                   user, service);
        return PAM_IGNORE;
    }

    /* 给用户一个可见提示（GDM greeter / sudo 终端都会显示 PAM_TEXT_INFO） */
    pam_info(pamh, "请看向摄像头进行人脸识别（%s）…", user);

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_alarm;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGALRM, &sa, NULL);

    pid_t pid = fork();
    if (pid < 0) {
        pam_syslog(pamh, LOG_ERR, "faceunlock: fork 失败: %s", strerror(errno));
        return PAM_IGNORE;
    }

    if (pid == 0) {
        /* 子进程：把 PAM 取到的身份信息交给助手，助手自己判断是否适用。
         *
         * 关键正确性措施：把子进程的 **stdout** 接到 /dev/null。
         *
         * PAM 应用可能把 stdout 当作自己的协议通道。最典型的是 polkit：
         * polkit-agent-helper-1 在 stdout 上写
         *     "PAM_TEXT_INFO <文本>" / "PAM_PROMPT_ECHO_OFF <文本>" / "SUCCESS"
         * 与 gnome-shell 的 polkit agent 通信。如果我们的助手往同一个 stdout
         * 打印任何普通文本，对方协议解析就会失败 —— 实测表现为
         * 「人脸每次识别都成功（score≈0.89），授权对话框却每秒重试一次、
         *   摄像头灯不停闪」，用户永远等不到授权通过。
         *
         * **stderr 同样不能想当然**（Ubuntu 26.04 / polkit 127 实测）：
         * 该版本把 polkit-agent-helper-1 起成 socket 激活的服务
         * （StandardInput/Output=socket, StandardError=inherit）。此时 stderr
         * 并不是一个可用的日志通道：
         *   * 助手写进去的文本在 journal 里**完全看不到**（对比 sudo 场景可见）；
         *   * 写操作会失败 —— 实测把 stderr 接到写不通的管道，助手（Python）
         *     正是以 **120** 退出（CPython 在退出阶段刷不出标准流时的专用码），
         *     而模块日志里就出现了 27 次 `helper_exit=120`。
         * 后果与污染 stdout 完全一样：gnome-shell 解析失败 → 每秒重建一次
         * 授权框 → pam_unix 拿到空密码/EOF（`conversation failed`、
         * `auth could not identify password`）→ 用户**根本来不及输密码**。
         *
         * 判据用 isatty(STDERR_FILENO)：
         *   * 终端场景（sudo / su）：保留 stderr，用户能看到
         *     "人脸识别通过（相似度 x.xx）"，是有用反馈；
         *   * 非终端场景（polkit socket、GDM、journal）：一律丢弃。
         * 结构化判定信息始终写 syslog（见 faceunlock/auth.py 的 _log），
         * 所以丢掉 stderr 不会损失可诊断性。
         */
        int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0) {
            dup2(devnull, STDOUT_FILENO);
            if (!isatty(STDERR_FILENO)) {
                dup2(devnull, STDERR_FILENO);
            }
            if (devnull > STDERR_FILENO) {
                close(devnull);
            }
        }
        const char *args[6];
        args[0] = helper;
        args[1] = user;
        args[2] = service;
        args[3] = rhost;
        args[4] = tty;
        args[5] = NULL;
        execv(helper, (char *const *)args);
        _exit(127);
    }

    g_child = pid;
    alarm((unsigned int)timeout);

    int status = 0;
    while (waitpid(pid, &status, 0) == -1) {
        if (errno != EINTR) {
            break;
        }
    }
    alarm(0);
    g_child = 0;

    int ret;
    if (WIFEXITED(status)) {
        switch (WEXITSTATUS(status)) {
        case 0:
            ret = PAM_SUCCESS;
            break;
        case 1:
            ret = PAM_AUTH_ERR;
            break;
        default:
            ret = PAM_IGNORE; /* 2 = 不适用，其余未知码也一律放行到下一模块 */
            break;
        }
    } else {
        /* 被信号杀死（含被本模块 alarm 超时 SIGKILL）: 视为不适用 */
        ret = PAM_IGNORE;
    }

    /* 120 = CPython 在退出阶段刷不出标准流（典型是 stderr 通道不可写）。
     * 单独点出来，免得下次再被当成"未知退出码"排查半天。语义上它和不适用
     * 是一回事：这里仍然只回退密码。 */
    if (WIFEXITED(status) && WEXITSTATUS(status) == 120) {
        pam_syslog(pamh, LOG_WARNING,
                   "faceunlock: 助手退出码 120（Python 写不出标准流，通常是 "
                   "stderr 通道不可用）-> 按不适用处理，回退密码");
    }

    if (debug || ret == PAM_IGNORE || ret == PAM_AUTH_ERR) {
        pam_syslog(pamh, LOG_NOTICE,
                   "faceunlock: user=%s service=%s helper_exit=%d -> %s",
                   user, service, WIFEXITED(status) ? WEXITSTATUS(status) : -1,
                   ret == PAM_SUCCESS ? "PAM_SUCCESS"
                                      : (ret == PAM_AUTH_ERR ? "PAM_AUTH_ERR"
                                                             : "PAM_IGNORE"));
    }
    return ret;
}

PAM_EXTERN int pam_sm_setcred(pam_handle_t *pamh, int flags, int argc,
                              const char **argv)
{
    (void)pamh;
    (void)flags;
    (void)argc;
    (void)argv;
    return PAM_SUCCESS; /* 本模块不建立任何凭据 */
}
