# RenWeave / 织译

> 理解故事，而不只是翻译句子。

RenWeave 是一个面向 Ren'Py 游戏的上下文感知多语言本地化引擎，可将任意源语言翻译为用户指定的任意目标语言。它以完整场景、剧情控制流和角色证据为核心，而不是将脚本拆成彼此无关的单行文本。

## 当前里程碑：0.1.0 Core

首个可运行核心已经实现：

- 从游戏根目录、`game` 目录或游戏程序识别 Ren'Py 项目。
- 安全读取并解包 RPA 2.0、3.0、3.2；受限反序列化索引并阻止路径穿越。
- 一键分析时只从 RPA 采集脚本类文件，避免无意义复制大型图片和音频资源。
- 发现 `.rpy`、`.rpyc`、`.rpa` 和已有 `game/tl/<language>`，并从源剧情索引中排除旧译文。
- 保留源文件哈希、编码、BOM 和换行信息。
- 将 `.rpy` 解析为稳定的场景与文本中间表示：对白、旁白、菜单和 `_()` UI 文本。
- 建立 `call`、`jump` 和文件内自然延续剧情边。
- 零 Token 生成角色出场证据、共现关系、剧情文件簇、场景摘要和重复专名候选。
- 为单个场景构建最小必要上下文，而不是注入整个世界观。
- 导入 OpenAI-compatible 模型配置，并通过环境变量读取密钥。
- 使用结构化场景翻译协议，按稳定文本 ID 接收译文。
- 校验缺失文本、未知 ID、Ren'Py 标签和变量/占位符。
- 将索引、知识、翻译结果、质量报告和断点状态原子写入独立工作目录。

当前版本尚未把模型译文写回 `game/tl`，也尚未接入 RPYC 反编译、AI 剧情时间线分析、全局精修和最终 RPA 构建。这些是下一阶段，不会以不安全的字符串替换提前实现。

## 快速开始

项目目前不依赖第三方运行库，要求 Python 3.10 或更高版本。

```powershell
python -m unittest discover -s tests -v
python -m renweave analyze "D:\Games\Example" --workspace "D:\RenWeaveWork\Example"
python -m renweave unpack "D:\Games\Example\game\scripts.rpa" --output "D:\UnpackedScripts" --scripts-only
```

使用源码目录直接运行时：

```powershell
$env:PYTHONPATH = "src"
python -m renweave analyze "D:\Games\Example" --workspace "D:\RenWeaveWork\Example"
```

模型配置示例位于 `examples/provider.openai-compatible.json`。密钥不写入 JSON：

```powershell
$env:RENWEAVE_API_KEY = "your-api-key"
$env:PYTHONPATH = "src"
python -m renweave provider-check examples/provider.openai-compatible.json
python -m renweave run "D:\Games\Example" `
  --workspace "D:\RenWeaveWork\Example" `
  --provider examples/provider.openai-compatible.json `
  --source-language ja `
  --target-language es-ES
```

目标语言没有内置白名单：可以使用 Ren'Py 目录标识、BCP 47 风格代码或模型能够理解的语言名称。`zh_hans` 只是简体中文项目可选的目标标识之一，不是产品默认值。

## 工作区产物

```text
workspace/
├── state.json
├── acquisition.json
├── acquired/
├── project-index.json
├── knowledge.json
├── translations/
│   └── scene_<id>.json
└── reports/
    └── scene_<id>.json
```

游戏原目录在当前阶段始终只读。所有 AI 结果必须通过结构校验后，未来的写回层才会生成翻译目录或补丁包。

## 核心原则

1. 场景是翻译单位，单行只是可写回的最小地址。
2. 世界观、角色和术语由软件自动发现，不是用户的开工表单。
3. 确定性解析优先于模型推理，以降低 Token 消耗。
4. 每个知识结论最终都应带源文件证据和剧情阶段。
5. 游戏源文件不可被翻译流水线直接覆盖。
