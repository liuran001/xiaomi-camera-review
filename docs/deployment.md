# 部署与维护

## Docker Compose

推荐 Linux x86_64、Docker Engine 和 Compose v2。NAS 的 Container Manager 需支持 Compose 的长格式 bind mount 及 `create_host_path: false`。其他架构尚未实机验证。预留约 1 GiB 容器内存及模型、数据库、缩略图和缓存空间。

```sh
python3 tools/init_config.py
python3 tools/download_model.py
```

编辑 `.env`：填写 `RECORDINGS_PATH`，检查容器 UID/GID，设置自己的 `APP_PASSWORD`。默认只监听本机；从其他设备访问时，把 `BIND_ADDRESS` 改成宿主机内网地址。不要将独立维护端口直接暴露到公网。

运行用户必须能够遍历并读取原片和模型目录、写入 `data/`。Linux 目录默认 0700。非 root 初始化使用当前 UID/GID；root 初始化会仅将本次新建的 `data/`、`models/` 交给配置中的容器 UID/GID（默认 1000:1000），不会递归更改已有目录。如果已经存在目录，或之后改用其他 UID，应由管理员仅调整应用的 `data/`、`models/` 权限，并为原片提供读取权限。不要递归修改摄像头共享文件夹的原有权限。

```sh
docker compose config --quiet
docker compose up --build -d
docker compose ps
docker compose logs --tail 100 web worker
```

用浏览器访问 `http://宿主机内网地址:8090/`；本机访问使用 `http://127.0.0.1:8090/`。输入 `.env` 中的 `APP_PASSWORD`。网页出现“后台在线”只说明 worker 存活，分析完成需查看进度。首次扫描至少等待一个稳定窗口。缺少模型会显示错误并重试，播放队列仍可工作。

录像命名需为 `通道_开始时间_结束时间.mp4`，例如：

```text
xiaomi_camera_videos/
  camera-A/
    00_20261016190000_20261016200000.mp4
    10_20261016190000_20261016200000.mp4
```

中间可以包含其他目录层级。同名通道位于不同目录时作为不同通道显示。时间格式为 `YYYYMMDDhhmmss`，结束时间须晚于开始时间；隐藏目录、`@` 开头目录、`#recycle` 和软链接不会被索引。

## 模型与离线安装

下载工具读取 [固定模型清单](../third_party/model-manifest.json)，验证 35,858,002 字节和 SHA-256，再原子替换目标；网络中断或校验失败保留旧文件并清掉本次临时文件。已存在正确模型时可完全离线执行。

无法访问上游时，可在可联网电脑按清单下载，将 `object_detection_yolox_2022nov.onnx` 放到 `models/`，再运行下载工具校验。离线构建 Docker 镜像还需要基础镜像、Debian 软件包和 Python 依赖；模型文件单独准备并不等于完整离线安装。上游镜像、包和模型均不绑定私人镜像源。

## 反向代理

普通部署应通过私网或支持 HTTPS 的代理访问。代理须传递真实的 `Host`，保留浏览器 `Origin`、Cookie、Range 和查询串，允许流式传输及适当的媒体超时；不要缓存认证后的录像响应。普通 Web 入口按根路径部署，任意子路径挂载尚不受支持。HTTPS 下设 `COOKIE_SECURE=1`。不要接受客户端伪造的 `X-Camera-DSM-*` 身份头。

QuickConnect 仅由 [DSM 集成](../synology/README.md) 提供，普通 Compose 端口不会自动获得此能力。

## 升级、备份和移除

升级前保存 `.env`、精确镜像引用和 `data/`；数据库使用 SQLite backup API，或先 `docker compose stop` 再复制整个 `data/`（包括 WAL/SHM）。运行中只复制 `catalog.sqlite3` 可能漏掉已提交的 WAL 数据。原片必须另行备份，`data/` 不是录像备份。

```sh
docker compose up --build -d
```

重建只管理当前项目的 web、worker，不需要重启 Docker 或 Container Manager。重启会保留已经提交的分析进度；回滚应使用预先记录的镜像及相容数据库版本，避免把旧数据库快照覆盖到更新的数据上。首次从没有真实时长字段的早期开发版本迁移，会重置旧人物进度并重新分析。

```sh
docker compose down
```

停止及移除容器不删除 bind mount 中的原片、数据库和模型。使用自动重启策略即可，无需 Tailscale 专用开机脚本。
