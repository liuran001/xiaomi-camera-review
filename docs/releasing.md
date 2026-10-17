# 发布

当前源码版本 `1.0.0`，DSM 集成包 build `1006`。源码采用 MIT；模型及依赖的授权另见 [第三方说明](../THIRD_PARTY_NOTICES.md)。

## 发布内容

源码 ZIP 包含通用 Docker 配置、Python/前端源码、测试、文档、固定模型清单及 DSM 集成源文件。不含模型权重、Docker 镜像、录像、人物图片、SQLite、凭据、会话材料、私人验收报告或旧 Git 历史。

```sh
python tools/build_release.py --output output/release
python tools/build_spk.py --output output/synology
```

源码包会生成相邻 `.sha256` 文件。源码构建使用明确白名单，排除旧 `Dockerfile.base`、Tailscale 开机脚本、现场报告和内部设计历史；拒绝所选文件或其父目录为软链接，缺少必要文件时停止。相同源码字节及同一 Python/zlib 工具链下输出可重复。

SPK 是管理员预部署后的 DSM 认证集成包，不是开箱即用安装器；不要把它描述成适用于任意 NAS 的一键安装发行版。基础镜像或第三方库更新会改变容器构建结果，源码 ZIP 的确定性不表示容器镜像完全可重复。

## 验证

```sh
python tools/download_model.py
python -m unittest discover -s tests -v
node --check static/app.js
node --check static/timeline.js
node --test tests/test_ui.js tests/test_timeline.js tests/test_dsm_bootstrap.js
docker compose config --quiet
docker compose up --build -d
```

真实模型、FFmpeg 和 FFprobe 必须齐全，发布测试不应出现 skip。从解压出的全新目录进行初始化、模型校验、容器启动，确认登录前 API 返回 401、登录后 API 可读、worker 心跳新鲜，以及只读录像挂载有效。新增 Docker 冒烟流程见 `.github/workflows/ci.yml`；上传仓库后才会由托管平台执行，工作流存在不代表已经运行通过。

现有运行逻辑与 DSM build 1006 曾在 DSM 7.4.1 x86_64 / Container Manager 24.0.2 完成实机回看、跳转、续播、撤权和清理验收。其他 DSM 版本及硬件仍需实机复验。

本次独立源码整理已从干净源码包解压目录验证：Windows Python 3.10 和 Linux 容器 Python 3.11 各 153 项测试、Node.js 24 的 56 项前端测试均通过，没有跳过项。通用 Compose 在 Docker Desktop Linux Engine 上完成公开基础镜像构建和非 root 容器启动；12 秒合成录像完成自动索引、YOLOX 分析、预览转码、原片/预览 Range 播放、退出后撤权验证，原片摘要未改变。只读录像/模型挂载、可写数据目录和 UID/GID 1000:1000 均实际核对。Linux root 初始化后的目录权限另经真实降权读写验证。测试使用合成素材，没有使用或附带私人录像。

源码 ZIP、SPK 和容器包含项目 MIT 声明及第三方说明。本次新构建的 SPK 未重新安装到生产 NAS；此前 DSM 实机验收针对运行逻辑，不等于此次归档字节的再次安装验收。GitHub Actions 工作流已准备，但尚未在远程托管平台执行。

## 公开托管

使用源码 ZIP 解压后的独立目录创建新 Git 历史，再上传自己选择的托管平台。不要把包含现场记录和私人地址的旧工程历史直接推送到公开仓库。首次仓库描述可使用：

> 面向小米 NAS 录像的私人回看与人物事件检索：北京时间时间轴、按需兼容播放和可选 DSM / QuickConnect 集成。

发布版本时同步检查 `VERSION`、Compose 镜像标签、更新记录；DSM 版本另需检查 `synology/INFO`、`tools/build_spk.py` 和 SPK 回归测试。先验证、再创建 tag；当前工作流只提供测试产物，不自动公开 Release、上传镜像或部署 NAS。
