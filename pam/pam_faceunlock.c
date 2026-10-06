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
         * stderr **保持原样**：polkit 不用 stderr 做协议（polkitd 会把助手的
         * stderr 收进日志），GDM 同理；而 sudo 场景下用户能在终端看到一行
         * "人脸识别通过（相似度 x.xx）"，是有用的反馈。结构性判定另外还会
         * 写 syslog（见 faceunlock/auth.py 的 _log）。
         */
        int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0) {
            dup2(devnull, STDOUT_FILENO);
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
