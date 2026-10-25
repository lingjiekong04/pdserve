# 分层复现

## 当前能复现到哪一层

| 层次 | 本轮状态 | 所需资源 |
|---|---|---|
| 字节/语法/单元测试 | 本地通过，见 CHECKS | Python 3.12；仅标准库 |
| 合成队列演示 | 本轮运行 | CPU，无模型 |
| 上游补丁重建 | 10个目标 after hash 全部匹配 | Git + 私有核验过的 stock 字节 |
| 聚合摘要/统计表 | 36次 raw 私有复算，公开统计保留 | CPU |
| 新目录完整依赖安装 | 未运行 | 包索引、Linux、匹配 wheel |
| 新便携入口真实推理 | 未运行 | 双GPU、模型、IB/RDMA |
| 历史双A100端到端服务/故障 | 历史证据已核验 | 原配置与历史环境 |
| 模型效果、真实业务、生产可用性 | 未验证 | 新评测与生产条件 |

## Windows：CPU 检查

执行位置：本地 Windows PowerShell，进入候选目录。

```powershell
Set-Location -LiteralPath "C:\src\PDServe"
python tools/check.py
if ($LASTEXITCODE -ne 0) { throw "CPU 检查失败，保留上方原始输出" }
python examples/mock_scheduler.py
if ($LASTEXITCODE -ne 0) { throw "合成示例失败" }
python tools/summarize_results.py
if ($LASTEXITCODE -ne 0) { throw "公开统计读取失败" }
python tools/check_release.py
if ($LASTEXITCODE -ne 0) { throw "公开发布检查失败" }
```

Windows 不用于执行 Mooncake/SGLang GPU 后端。安装本地 package 不是 CPU 检查前提。

## Linux：新环境安装（用户选择执行，本轮未运行）

执行位置：Linux 终端，独立候选目录。不要在原实验目录或原 `.venv` 中执行。

```bash
cd /path/to/PDServe-public-candidate-20261025
python3.10 -m venv .venv-gpu
source .venv-gpu/bin/activate
python -m pip install pip==25.1.1 setuptools==75.8.0 wheel==0.45.1
python -m pip install torch==2.6.0+cu124 torchvision==0.21.0+cu124 \
  --index-url https://download.pytorch.org/whl/cu124
python -m pip install flashinfer-python==0.2.5+cu124torch2.6 \
  --find-links https://flashinfer.ai/whl/cu124/torch2.6/flashinfer-python/
python -m pip install -r requirements/gpu.txt -c requirements/gpu-constraints.txt
python -m pip check
```

以上是固定版本安装配方，不是本轮重新安装成功的记录。FlashInfer wheel 曾需要本地上传；网络、旧 wheel、Python ABI、系统驱动与 IB 库可用性需由执行者检查。`requirements/historical-linux.lock` 是历史实测 freeze，包含 CUDA 依赖；不应该作为 Windows 安装文件。约束文件和最终 freeze 在非关键包版本上可能不同；最终 freeze 描述历史实际环境。禁止用无记录的“升级到最新版”替代后声称复现同一次实验。

## 模型

仅使用本地 Qwen2.5-7B-Instruct：

```bash
export PDSERVE_MODEL_PATH=/absolute/path/to/Qwen2.5-7B-Instruct
python tools/verify_model.py "$PDSERVE_MODEL_PATH"
```

校验工具读取文件进行 SHA256，不加载模型。已有模型来源是本地文件；上游 revision 未确认。官方资源见 THIRD_PARTY。公开 manifest 不含原存储路径；文件哈希保留。不同下载版本若不能匹配，应记录为新实验，不修改历史哈希。

## 固定上游补丁重建

可从新安装的包目录复制，或从固定 tag 的 `python/` 目录重建。以下命令只创建新的 checkout 和 patched-site：

```bash
# 在候选目录；work/upstream-sglang 事先不存在
mkdir -p work
git clone --depth 1 --branch v0.4.6.post5 \
  https://github.com/sgl-project/sglang.git work/upstream-sglang
python tools/rebuild_engine.py \
  --upstream work/upstream-sglang/python \
  --output work/patched-site
```

本轮验证使用私有证据中的 stock 文件，不是本轮重新下载的 Git tag。脚本严格检查 7 个原文件 before hash、3 个新增文件的缺失状态和全部10个 after hash；下载字节不同会明确失败。不要刷新 manifest 来绕过失败。

```bash
export PYTHONPATH="$PWD/src:$PWD/work/patched-site:$PWD/benchmarks"
```

## 真实服务：三个独立终端

每个终端都在同一候选目录、激活同一个 `.venv-gpu`，设置同样 `PDSERVE_MODEL_PATH/PYTHONPATH`。

```bash
# 只检查将要执行的命令；不会调用GPU
python scripts/serve_backend.py prefill --model-path "$PDSERVE_MODEL_PATH" --dry-run
# 终端1，真正启动GPU0
python scripts/serve_backend.py prefill --model-path "$PDSERVE_MODEL_PATH"
# 终端2，真正启动GPU1
python scripts/serve_backend.py decode --model-path "$PDSERVE_MODEL_PATH"
# 终端3，两个后端就绪后
python -m pdserve.launch fair --model-path "$PDSERVE_MODEL_PATH"
```

FIFO 对照把 `fair` 换为 `fifo`。双混合副本对照先手动停止上述三个进程，分别运行 `replica0`、`replica1`，网关改为：

```bash
python -m pdserve.launch_mixed --model-path "$PDSERVE_MODEL_PATH"
```

公开启动器默认只使用 GPU0/1、回环地址、context16384、BF16、禁用radix、max-running32、seed2026、Mooncake。它不自动选择设备、不启动整个堆栈、不杀旧进程、不自动重启。正常停止在各自终端使用 Ctrl+C，然后检查端口和资源。

## 新实验

需要真实服务已就绪。使用新 campaign 名称，不把当前候选的观测工具混入旧数据。

```bash
python scripts/preflight.py pd_fair --model-path "$PDSERVE_MODEL_PATH"
python benchmarks/run_pilot.py pd_fair --campaign my-new-run
python benchmarks/run_matrix.py pd_fair --campaign my-new-run
```

混合和 FIFO 切换后重复相同 campaign。candidate campaign 会记录物理GPU、boot、依赖/model manifest与公开版本清单，变化时拒绝续跑。新测试产生的本机标识留在忽略的 reports 下，不默认公开。脚本保留失败 sidecar；失败尝试应单独报告。该路径本轮只作语法/CPU检查，没有新GPU实测。
