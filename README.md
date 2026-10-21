# 时光 · 小米录像回看

把摄像头自动上传到 NAS 的录像变成可按时间回看、按人物事件检索的私人录像库。直接读取已有 MP4，不绑定米家账号，不整体重压缩原片。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green)

- 北京时间全天时间轴，输入时间跳转，同通道多文件及跨午夜接续。
- 按需转成浏览器可播放的 H.264/AAC 短片，复用已转码区间，预加载下一片。
- CPU 抽样检测人物出现，可选空场景加速；后台可暂停，已提交进度可断点恢复。
- 通过目录变更通知发现新录像，观察大小和修改时间稳定后再分析；支持可选固定文件大小校验。
- 原片只读挂载，独立登录，同源 API，临时文件恢复清理及 MP4 缓存预算。
- 可选 DSM 原生会话与 QuickConnect 网关；所有有效登录 DSM 的用户可进入。

这是人物出现检测，没有人脸身份识别，也不接入实时直播。抽样可能漏检或误报，冷缓存跳转仍需等待转码。界面和事件时间轴固定显示北京时间。

Linux 上没有录像变化时不周期遍历原片目录或清理缓存。心跳存放在 web、worker 共享的 tmpfs 内存卷，空闲队列通过 SQLite 版本变化检测新请求，避免持续读写业务表。上传稳定检查和任务重试保留；监听失效时自动回退定时扫描。

## 快速开始

推荐 Linux x86_64、Docker Engine、Compose v2，宿主机需有 Python 3.10+ 用于初始化和模型下载。摄像头文件名须类似 `00_20261016190000_20261016200000.mp4`，可以放在摄像头子目录中。

```sh
python3 tools/init_config.py
python3 tools/download_model.py
# 编辑 .env：填写录像绝对路径、UID/GID，并设置自己的访问密码。
# 跨设备访问还需把 BIND_ADDRESS 改成宿主机内网地址；默认只监听本机。
docker compose config --quiet
docker compose up --build -d
```

浏览器打开 `http://127.0.0.1:8090/`，使用 `.env` 的 `APP_PASSWORD` 登录。从其他设备访问时使用配置的内网地址。初始化生成随机密码和会话密钥，不覆盖已有 `.env`；原片、模型必须可读，`data/` 必须可写。

模型首次下载约 36 MB，按固定版本、字节数和 SHA-256 验证后才投入使用。Dockerfile 可从公开基础镜像构建，不依赖私人基础镜像。详细权限、离线安装、反向代理、升级和备份步骤见 [部署文档](docs/deployment.md)。

## 文档与目录

| 内容 | 入口 |
| --- | --- |
| 安装、维护、备份 | [部署](docs/deployment.md) |
| 全部环境变量及文件稳定规则 | [配置](docs/configuration.md) |
| 数据流、调度和缓存 | [架构](docs/architecture.md) |
| API 契约 | [API.md](API.md) |
| DSM / QuickConnect 集成边界 | [synology/README.md](synology/README.md) |
| 测试与贡献 | [CONTRIBUTING.md](CONTRIBUTING.md) |
| 发布、验证和兼容性 | [发布说明](docs/releasing.md) |
| 安全和访问范围 | [SECURITY.md](SECURITY.md) |

`camera_review/` 为 Python 服务，`static/` 为无需前端构建的界面，`tests/` 为回归测试，`tools/` 为初始化、模型下载和打包工具，`synology/` 为可选 DSM 集成，`third_party/` 为模型清单和许可证。运行时的 `.env`、`data/` 和 `models/` 均不纳入版本库或发布包。

## 当前适用范围

运行架构是一个 web 进程和一个 worker；不要增加并行 worker。普通部署采用保守资源默认值，无固定 CPU 绑定，管理员可按机器情况调整线程数。定位缓存上限 512 MiB、播放缓存 1.5 GiB，数据库、缩略图和处理中临时文件另计。单个原片大于 512 MiB 的定位处理目前不支持。

DSM build 1006 在 DSM 7.4.1 x86_64 / Container Manager 24.0.2 上完成过实机验收。现有 SPK 是需要管理员先部署应用的集成包，不是任意 NAS 上直接安装即用的完整发行套件。通用 Docker 配置面向新部署，不能直接覆盖已有 DSM 套件管理的项目。

## 许可证

项目源码采用 [MIT](LICENSE)。YOLOX 模型及容器内其他组件保留各自许可证，见 [第三方说明](THIRD_PARTY_NOTICES.md)。
