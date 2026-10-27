# Coder390 Mac

把已有的 Coder390 GGUF 接到优化后的 llama.cpp，在 Apple Silicon Mac 上启动本地聊天界面和 OpenAI 兼容 API。

**用户自备模型。本仓库不会下载、转换或上传模型权重。** 首次运行只准备固定版本的引擎源码、CMake 和官方聊天界面，并在用户自己的 Mac 上编译。

> 预览版。优化方案与 Q3/Q8 在 M2 Ultra 上已有实测；M1 Max 32GB 提供保守配置，但尚未经过该硬件的实机验收。请不要把 M2 Ultra 的速度当作 M1 Max 的速度。

## 开始使用

需要原生 Apple Silicon macOS、Python 3.9 或更新版本、至少 32GB 统一内存、Apple Command Line Tools，以及约 4GiB 的构建空间（不含已有模型）。Apple 开发工具提供可用的 Python；若尚未安装，先运行并完成系统安装窗口：

```sh
xcode-select --install
```

下载项目，然后直接指定本地模型文件。路径有空格时保留引号：

```sh
git clone https://github.com/chenwenhao0326-arch/coder390-mac.git
cd coder390-mac
python3 coder390.py run --model "/你的模型目录/Qwen3.8-27B-Coder390-EfficientThink-Q3LynnStyle-MTP.gguf"
```

首次运行会检查模型、校验下载的构建组件、编译引擎并打开 `http://127.0.0.1:8793/`。后续直接复用构建结果。终端需要保持打开，按 **Ctrl+C** 关闭本次启动的模型。

页面右上角有 **“模型设置”**：可选择实际上下文容量、开启看图并填写视觉投影路径。点击 **“应用并重启”** 后，页面显示进度，完成后刷新；不需要回到终端修改参数。模型正在生成时拒绝切换，已有聊天记录和未发送文字会保留。未发送附件请先处理。

已有用户先停止旧服务，再在仓库目录运行 `git pull`，然后重新执行启动命令。请使用启动脚本输出的新链接进入；升级只处理 llama-ui 静态资源缓存，不删除聊天数据库。

模型文件始终留在原位置，不复制进项目。没有单独安装 Homebrew、Node.js、pip 依赖或商业 API 的要求。若编译器不支持固定版本的 Metal 源码，脚本会明确报错并保留构建日志；不同 macOS/SDK 版本尚未逐一验收。

## 支持的模型

面向作者 Merkyor / nerkyor 的 **Qwen3.8-27B Coder390 EfficientThink** 单文件 GGUF，要求内置一个 MTP 层，词表、特殊 token、聊天模板、架构和张量形状与已验证版本一致。启动前会检查这些项目；它不是所有 Qwen/GGUF 模型的通用启动器。

32GB 建议使用已经验证能够被本引擎加载的标准 Q3 包：

```text
Qwen3.8-27B-Coder390-EfficientThink-Q3LynnStyle-MTP.gguf
```

该文件约 16.1GiB，SHA256：

```text
9985b3d41fc7dc0bb5d493f7523d4515504912a3a7fc830f66e0fd2f90f9fd95
```

这里的 Q3 是混合量化，内置草稿头为 Q4；`MTP4` 是草稿长度 4，不是量化位数。32GB 配置不接受约 27GiB 的 Q8 文件。

模型由用户自行从[作者 ModelScope 仓库](https://modelscope.cn/models/Merkyor/Qwen3.8-27B-Coder390-EfficientThink-Opus5.5-GPT6Astra-Grok4.7-DSV4Pro-K3-SFT-RLOO-GGUF-NInfer)或对应 Hugging Face 仓库取得，遵循模型自己的许可证。脚本检查结构，不替代用户的完整文件 SHA256 校验。

## 默认配置

| 配置 | 自动选择 | 上下文 | 并发 | batch / ubatch | 默认输出预算 |
|---|---|---:|---:|---:|---:|
| `compact32` | 32～36GB | 8192 | 1 | 512 / 128 | 2048 |
| `balanced` | 大于 36GB | 32768 | 1 | 2048 / 512 | 4096 |

两者均使用 Metal 全层卸载、Flash Attention、F16 KV、概率 MTP4、p_min 0.2、131072 项草稿词表、紧凑 Top-K 和已采用的小算子线程分配优化。原模型的完整词表和权重不变。

32GB 从 8K 上下文开始，页面可选4K、8K、16K；更大内存配置提供更多档位，应用前检查保守内存预算。窗口改变后服务端缓存会重新建立，较小窗口可能需要新建对话。M2 Ultra测试不能替代M1 Max32GB实机验收。

输出预算较短，复杂思考可能被截断，需要结合任务调整；API调用方或界面中已有的个人设置可以覆盖默认输出预算。页面设置会保存，下次启动同一模型/配置时恢复；显式命令行参数优先。

```sh
python3 coder390.py run --model "/路径/模型.gguf" --profile compact32 --context 8192
```

## 开启看图（包括32GB配置）

模型主文件和视觉投影是两个文件。用户自行准备匹配的 `mmproj-Qwen3.8-27B-Q8_0.gguf`（约600MiB）。放在主模型同目录时自动识别并开启；不同目录可指定：

```sh
python3 coder390.py run --model "/路径/模型.gguf" --mmproj "/路径/mmproj-Qwen3.8-27B-Q8_0.gguf"
```

也可以先启动，在页面 **模型设置 → 视觉投影文件路径 → 启用看图 → 应用并重启**。路径指这台运行模型的Mac上的文件，不是上传权重到网页。首次完整校验投影SHA256；文件未改变时复用校验回执。

没有投影文件时保持文字模式，页面会提示填写，不会下载权重或假装看图已开启。32GB配置限制图片处理规模至1024个图像token，并监控模型进程RSS；超过预算会停止所属模型并显示原因，不调整系统内存限制。图片越大、越多，实际资源需求越高。

本版已验收图片输入；不宣称已验证视频、音频或所有多模态场景。需要关闭看图可在面板取消勾选，或启动时加 `--no-vision`。

若新配置加载失败，会尝试恢复此前工作的配置；无效路径、校验失败或生成中的切换会在停止旧模型前被拒绝。

## 网络、离线和诊断

首次准备只下载约 87MB 的公开构建组件，来源和 SHA256 固定在 `engine.lock.json`。使用物理网卡直连，显式禁用代理；逐跳检查连接，不自动更换代理或修改系统路由。默认自动查找物理网卡，可手动指定：

```sh
python3 coder390.py setup --interface en0
python3 coder390.py doctor --model "/路径/模型.gguf"
```

本机网络如果要求 VPN/代理，自动直连可能失败。可在能访问官方地址的环境自行取得 `engine.lock.json` 中的三个文件，保持文件名不变放到缓存目录，使用：

```sh
python3 coder390.py setup --offline --cache-dir "/路径/构建组件缓存"
python3 coder390.py run --offline --model "/路径/模型.gguf"
```

准备完成后启动和推理不访问模型下载服务。日志与临时文件只在 `.coder390/`，已排除 Git：

- `build.log`：编译诊断。
- `server.log`：模型加载、MTP 和请求耗时。
- `last-run.json`：本次准确启动参数。
- `user-settings.json`：保存的上下文和看图设置，仅在本地。

默认仅监听本机。端口被占用时会停止启动，不会结束别的服务；也可用 `--port 8795`。`--no-open` 不自动打开浏览器。

API 地址为 `http://127.0.0.1:8793/v1`，模型别名 `coder390`。本项目不包含 DSH、Agent 会话或模型下载器。

页面与API使用固定本机端口；模型运行在管理器持有的私有本机端口，重启期间新推理请求返回503。设置接口要求同源和会话令牌，防止外部网页擅自重启模型；服务端API客户端无需额外配置管理令牌。

## 复现与验证

引擎固定在 llama.cpp `620d2225cc436ab0b001e3200e590e3b286ef9ab`，依次应用 `patches/` 中三份补丁。补丁后源码对照已采用版本的摘要；编译发生在用户机器上，不分发本机 M2 编译出的二进制给 M1 使用。

```sh
python3 -m unittest discover -s tests -v
```

具体已测项目和未测边界见 [v0.2验证记录](docs/VALIDATION-v0.2.md)；[v0.1记录](docs/VALIDATION.md)保留。性能数据见 [实测摘要](docs/PERFORMANCE.md)。Node.js仅用于可选的服务工作线程回归检查：`node tests/sw-lifecycle.mjs`，不是运行依赖。

## 许可与来源

启动脚本和本项目改动使用 MIT；llama.cpp 原有版权和许可保留在 `licenses/llama.cpp-MIT.txt`。引擎、CMake、聊天界面和用户模型分别保留各自许可证，见 [来源说明](NOTICE.md)。本仓库不包含账号凭据、模型文件或开发会话日志。
