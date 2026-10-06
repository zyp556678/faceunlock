# 关于本仓库中的隐私与生物特征数据

本仓库**不包含任何真实人脸图像、人脸特征向量（embedding）或模板文件**。

## 已排除的内容

以下路径通过 `.gitignore` 排除，不会进入版本库：

| 路径 | 内容 | 排除原因 |
|---|---|---|
| `test_output/` | 开发期截图（`gui_*.png`）、`smoke_annotated.jpg`、`negatives/` 测试人像、`smoke_feats.npy` | 含开发者本人人脸照片与由真人照片计算出的 128 维特征 |
| `vendor/`、`vendor-wheels/` | 第三方 OpenCV 二进制 | 大体积构建产物 |
| `models/*.onnx` | YuNet / SFace 模型权重 | 大体积上游产物，README 说明获取方式 |

`/var/lib/faceunlock/`（真实模板库）位于系统路径，不在仓库内。

## 关于示例中的用户名

README、文档与测试脚本中出现的用户名（`alice` 等）均为**占位符**，不是任何真实账户。
测试脚本通过 `$USER`、`FU_TEST_USER`、`FACEUNLOCK_GUI_USER` 等环境变量取当前用户。

## 仓库中的实测数据说明

README 与源码注释中引用了若干识别分数（如「本人 0.86~0.96 / 冒充者 0.092」）。
这些是**标定用的统计数字**，不是可还原生物特征的数据——128 维特征向量不可逆，
且这些数字本身不足以重建任何人的人脸。

## 如果你要发布基于本项目的派生作品

请务必自行确认：

1. 不要提交 `test_output/`、`/var/lib/faceunlock/` 或任何含人脸的图像；
2. 不要提交 `/etc/faceunlock/config.json` 与 `secret.key`（HMAC 密钥，泄露即可伪造模板）；
3. 若引用他人的测试人像，注意肖像权与著作权（`test_output/negatives/` 中的名人照片
   仅用于本机冒充测试，未随仓库分发）。
