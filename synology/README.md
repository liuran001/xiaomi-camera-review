# DSM 套件

此目录为可选 DSM 原生认证集成。当前 SPK 依赖 Container Manager，接管预先部署在固定路径 `/volume1/docker/xiaomi-review` 的应用。它不包含 Docker 镜像、模型、录像或首次安装向导，不能在未预部署的任意 NAS 上直接安装即用。新用户请先使用主 README 的通用 Docker 部署；需要 DSM 免密和 QuickConnect 时，再由熟悉 DSM 的管理员按下列条件准备专用原生项目。通用 Compose 与原生项目不能同时管理同一套应用。

构建命令：`python tools/build_spk.py --output output/synology`。完成全部预部署条件后，可在套件中心手动安装输出的 `.spk`。当前仅在 DSM 7.4.1-90080 Update 1、x86_64、Container Manager 24.0.2-1706 上有实机验收；其他 DSM 或架构需要复验。

DSM 7 以独立套件用户运行脚本，容器由 `conf/resource` 的 `docker-project` 原生资源管理。首次接管前，管理员需要备份 SQLite（包括已提交的 WAL）、`.env`、原 Compose、镜像引用及旧启动脚本，再准备：

- 已有应用标记 `.codex-camera-review`。
- `dsm-project-template.json`，仅包含 web、worker，保留原只读录像挂载、数据、模型、UID、资源限制及镜像。敏感环境变量通过已有 `.env` 引用。
- `.dsm-package` 父目录模式 0711；其中 `runtime` 归套件 UID/GID 所有、模式 0700。套件内 `project` 链接到该目录下的项目。

通用 Compose 的自动容器名称不能原样用作模板：原生项目要求 `web`、`worker` 两服务及 `xiaomi-review-web`、`xiaomi-review-worker` 两个固定容器名称，所有挂载和 `.env` 路径应解析为宿主机绝对路径，并设置已构建镜像。套件脚本不会自动完成项目接管、目录所有者初始化或迁移备份。`package_control.py` 的旧 root 引导辅助类还包含部署特定 UID，不是新安装工具；当前套件钩子只使用 `PackageManager`。

原生项目转换 JSON/YAML 时会将纯数字字符串转成数字。当前模板将 worker 命令写为 `nice -n15 python -m camera_review.worker`，web CPU 集合写为 `3-3`。保留这一写法，避免 Container Manager 校验失败。

签名密钥由套件生成，位于私有 runtime 中，文件 0444、父目录 0700；web 仅通过只读文件挂载读取。升级保留密钥和数据，原生资源负责容器生命周期。状态脚本验证网页服务，分析 worker 是否正常还应查看网页“后台在线”、心跳及容器状态。

DSM 网关逐次调用原生 `authenticate.cgi`，只将绑定当前请求的 HMAC 身份传给回看后端。DSM Cookie 不进入容器。桌面应用使用同源地址，新窗口打开；QuickConnect 的此路径需按当前 DSM 和中继版本实测，不代表任意第三方端口受支持。

入口使用当前 DSM 站点的 `/webman/3rdparty/XiaomiCameraReview/index.cgi/`，不需要在代码中填写私人 QuickConnect 地址。所有有效登录 DSM 的用户均可查看整套录像库，不按共享 ACL 分别过滤；这不是只给管理员开放的入口。

桌面入口使用 DSM 当前仍支持的 `legacy` 窗口启动类型。实机通用 `url` 类型会在 QuickConnect 下被客户端第三方 URL 检查拦截，即使目标是已经可达的 DSM 内部路径；`legacy` 直接在新窗口打开同一受认证路径，不修改群晖白名单、认证或中继配置。该兼容方式已按当前 DSM 版本测试，升级 DSM 后应复验。

普通 DSM 网页会话还需要原生 SynoToken。入口从 DSM 官方同源令牌接口取得它，通过严格同源 POST 验证后，存入仅浏览器会话有效、限本入口路径的 HttpOnly Cookie（HTTPS 下 Secure、SameSite Strict），供无法自行设置请求头的视频和图片使用。官方建议将令牌仅放 JavaScript 内存；这里采用 Cookie 仅作传输适配，不建立独立登录身份，每次仍以当前 DSM 会话向原生接口重新校验。退出 DSM 后旧令牌不能继续访问。细节见 [原生令牌接入](../docs/dsm-native-token-bridge.md)。

回滚时保留当前数据库，使用接管前保存的精确镜像和配置恢复原两个容器。不要用旧 SQLite 快照覆盖正在增长的分析数据库。
