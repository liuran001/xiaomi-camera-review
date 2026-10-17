# 开发与贡献

Python 3.10+、Node.js 20+、FFmpeg/FFprobe（含 libx264）用于测试；容器使用 Python 3.11。推荐 Linux，应用测试也可在 Windows 执行。

```sh
python -m venv .venv
# Linux/macOS: . .venv/bin/activate
# PowerShell: .venv/Scripts/Activate.ps1
python -m pip install -r requirements.txt
python tools/download_model.py
python -m unittest discover -s tests -v
node --check static/app.js
node --check static/timeline.js
node --test tests/test_ui.js tests/test_timeline.js tests/test_dsm_bootstrap.js
```

缺少模型或 FFmpeg 会跳过部分真实媒体测试；准备发布时必须安装两者并确认没有 skip。测试仅使用合成视频和临时目录，不需要生产录像或 DSM 账号。DSM 生命周期和网关自动测试使用替身；QuickConnect 兼容性仍需指定 DSM 版本的实机验收。

源码启动时分别运行 `gunicorn --bind 127.0.0.1:8090 --workers 1 --threads 4 --timeout 90 camera_review.web:app` 和 `python -m camera_review.worker`。先导出 `APP_PASSWORD`、`SESSION_SECRET`、`RECORDINGS_ROOT`、`DATA_ROOT`、`MODEL_ROOT`；路径使用本机绝对路径。`.env` 仅供 Compose 插值，不会被应用自动加载。Windows 开发建议使用容器运行 Gunicorn。

提交前确保原片只读、源版本变化不能提交旧结果、所有 API/媒体身份验证仍有效、时间轴不受浏览器系统时区影响。添加功能时优先扩展现有回归测试。不要提交 `.env`、录像、人像、SQLite、模型权重、会话材料或含私人地址的验收记录。

发布流程见 [发布说明](docs/releasing.md)。
