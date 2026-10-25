# Third-party sources and licenses

| Component | Pinned baseline | Role / distribution boundary | License evidence |
|---|---|---|---|
| [SGLang](https://github.com/sgl-project/sglang/tree/v0.4.6.post5) | 0.4.6.post5 | External engine; only a consolidated patch is shipped | Installed distribution METADATA and LICENSE reviewed; Apache-2.0 text in LICENSES |
| [Mooncake](https://github.com/kvcache-ai/Mooncake/tree/v0.3.2) | 0.3.2 | External KV transport; no engine/library binary shipped | Upstream project link; dependency license remains upstream |
| [FlashInfer](https://github.com/flashinfer-ai/flashinfer/tree/v0.2.5) | 0.2.5+cu124torch2.6 wheel | External CUDA attention kernels; not bundled | External dependency; no new attribution of implementation |
| [PyTorch](https://pytorch.org/) | 2.6.0+cu124 | External tensor runtime; not bundled | External dependency |
| [Qwen2.5-7B-Instruct](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct) | model revision unverified | Model/tokenizer files are not distributed | Official model card identifies Apache-2.0; local LICENSE hash in resources |

The SGLang before hashes correspond to the exact historical installed stock source. The upstream tag is a locator, not a claim that a newly downloaded checkout was tested this round. `tools/rebuild_engine.py` refuses an unrecognized baseline and checks all final hashes.

The patch changes 7 existing upstream files and adds 3 PDServe engine modules. Changed-file paths, before/after hashes and consolidated-diff notice document modifications; upstream copyright headers are retained in patch context and reconstructed code. The complete upstream source tree is not included. Candidate-specific modifications and original modules are offered under Apache-2.0. No individual author's identity is invented.

The official SGLang tag, Mooncake tag and Qwen model card were opened during publication review. Model revision and present wheel-download availability remain gaps. No redistributed weights are covered by the project's LICENSE by implication.
