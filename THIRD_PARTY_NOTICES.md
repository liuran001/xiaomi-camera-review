# 第三方组件

| 组件 | 用途 | 授权 / 来源 |
| --- | --- | --- |
| OpenCV Zoo YOLOX ONNX | CPU 人物出现检测 | Apache-2.0；[固定清单](third_party/model-manifest.json)及 [YOLOX 许可证](third_party/YOLOX-LICENSE)，含 Megvii 原版权声明 |
| Flask | HTTP 应用 | BSD-3-Clause |
| Gunicorn | WSGI 服务 | MIT |
| NumPy | 数组与图像处理 | BSD-3-Clause |
| OpenCV Python headless | DNN 推理、图像处理 | Apache-2.0；二进制包含的其他组件见对应 wheel 的 LICENSE 文件 |
| tzdata | 跨平台时区数据 | Apache-2.0；IANA 时区数据另见其上游说明 |
| FFmpeg | 解码、重封装和播放转码 | 随构建选项为 LGPL/GPL；Debian 包包含 libx264 支持，分发容器时须保留其对应许可证及源码获取信息 |
| tini | 容器进程回收 | MIT |
| Python / Debian | 容器运行环境 | 各自及所含软件包的许可证 |

Python 依赖版本见 `requirements.txt`。权重不随源码或 SPK 打包，由用户通过带 SHA-256 校验的工具取得。模型阈值仅经过有限场景验证，不代表不同摄像头、光线和位置的准确率保证。

`third_party/MobileNet-SSD-LICENSE` 仅保留早期实验模型的许可证记录；当前运行使用 YOLOX，不下载或加载 MobileNet SSD。

项目名称只描述适用的录像格式，与小米、群晖及以上组件维护者没有官方隶属或背书关系。重新分发 Docker 二进制镜像时，应核对镜像内全部依赖的许可证；本项目的源码许可证不会替代第三方义务。
