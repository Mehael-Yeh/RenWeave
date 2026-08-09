# RenWeave / 织译

> 理解故事，而不只是翻译句子。

RenWeave 是一个面向 Ren'Py 游戏的上下文感知多语言本地化引擎，可将任意源语言翻译为用户指定的任意目标语言。它以完整场景、剧情控制流和角色证据为核心，而不是将脚本拆成彼此无关的单行文本。

## 当前里程碑：0.5.0 Global Refinement

首个可运行核心已经实现：

- 从游戏根目录、`game` 目录或游戏程序识别 Ren'Py 项目。
- 安全读取并解包 RPA 2.0、3.0、3.2；受限反序列化索引并阻止路径穿越。
- 一键分析时只从 RPA 采集脚本类文件，避免无意义复制大型图片和音频资源。
- 发现 `.rpy`、`.rpym`、`.rpyc`、`.rpymc`、`.rpa` 和已有 `game/tl/<language>`，并从源剧情索引中排除旧译文。
- 自动识别缺少对应源码的 `.rpyc` / `.rpymc`，在独立工作区中反编译并纳入场景索引。
- 按需下载固定提交的 unrpyc 2.0.2，验证 SHA-256，并限制下载体积、解压体积和归档路径。
- 保留源文件哈希、编码、BOM 和换行信息。
- 将 `.rpy` 解析为稳定的场景与文本中间表示：对白、旁白、菜单和 `_()` UI 文本。
- 建立 `call`、`jump` 和文件内自然延续剧情边。
- 零 Token 生成角色出场证据、共现关系、剧情文件簇、场景摘要和重复专名候选。
- 按剧情文件簇分块提炼带场景证据的世界事实、角色定位、说话风格、关系和术语含义。
- 对剧情簇摘要做层级合并，避免把整部游戏一次性塞进模型上下文。
- 以请求内容、模型和提示词哈希缓存知识结果；换目标语言时可直接复用，不重复消耗 Token。
- 为单个场景构建最小必要上下文，而不是注入整个世界观。
- 每场只注入相关剧情、出场角色、命中术语和短全局摘要，并限制证据数量与字段长度。
- 导入 OpenAI-compatible 模型配置，并通过环境变量读取密钥。
- 使用结构化场景翻译协议，按稳定文本 ID 接收译文。
- 校验缺失文本、未知 ID、Ren'Py 标签和变量/占位符。
- 将索引、知识、翻译结果、质量报告和断点状态原子写入独立工作目录。
- 根据 Ren'Py 的标准标识算法生成场景级 `translate` 对话块，而不是修改游戏源脚本。
- 为菜单和 `_()` UI 文本生成全局 `old/new` 字符串翻译，并自动统一重复原文的译法。
- 校验失败时只把问题文本及错误代码发送给模型修复，默认最多两次，不重复消耗整场景上下文 Token。
- 将 `es-ES` 等目标代码规范化为合法的 Ren'Py 语言标识（例如 `es_es`）。
- 在完整翻译通过校验后自动生成 `output/game/tl/<language>` 可安装目录。
- 可选一键安装；默认拒绝覆盖非 RenWeave 创建的同名翻译文件。
- 在初译全部通过结构校验后执行跨场景风险审计，只挑选重复原文译法冲突、疑似漏译、异常长度、术语命中和少量角色声线样本。
- 将风险文本按长度与数量上限分批交给模型精修，不把已经可信的整部译文重新发送，从架构上控制额外 Token 消耗。
- 精修请求按模型、提示词和实际候选内容缓存；相同项目重复运行可直接复用结果。
- 对模型提出的修改按场景再次校验 Ren'Py 标签、插值变量、占位符和文本 ID；批量修改失败时自动逐条隔离并拒绝不安全修改。
- 将精修候选数、实际修改、拒绝项、模型调用数和缓存命中数写入独立报告及断点状态。

当前版本尚未接入精细剧情时间线、Ren'Py 编译校验、最终 RPA 构建和桌面一键界面。这些是下一阶段，不会以不安全的源文件字符串替换提前实现。

## 快速开始

项目目前不依赖第三方运行库，要求 Python 3.10 或更高版本。

```powershell
python -m unittest discover -s tests -v
python -m renweave analyze "D:\Games\Example" --workspace "D:\RenWeaveWork\Example"
python -m renweave unpack "D:\Games\Example\game\scripts.rpa" --output "D:\UnpackedScripts" --scripts-only
python -m renweave decompile "D:\Games\Example" --workspace "D:\RenWeaveWork\Example"
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
  --target-language es-ES `
  --install
```

不使用 `--install` 时，完整语言包只会生成到工作区，不修改游戏。之后可以从已有的验证结果重新构建或安装，无需再次消耗模型 Token：

```powershell
python -m renweave build --workspace "D:\RenWeaveWork\Example" --install
```

目标语言没有内置白名单：可以使用 Ren'Py 目录标识、BCP 47 风格代码或模型能够理解的语言名称。`zh_hans` 只是简体中文项目可选的目标标识之一，不是产品默认值。目标名称会被转换为可用作 Ren'Py 标识符的运行时语言名。

当项目只有编译脚本时，`analyze` 和 `run` 会自动获取并运行固定版本的 [unrpyc](https://github.com/CensoredUsername/unrpyc)。使用 `--no-tool-download` 可强制离线；也可用 `--unrpyc D:\Tools\unrpyc.py` 或环境变量 `RENWEAVE_UNRPYC` 指定已有工具。反编译在隔离子进程和工作区副本中执行，不会把 `.rpy` 写回原游戏。由于 RPYC 本质上包含序列化数据，仍应只处理来源可信的游戏文件。

`run` 默认自动建立 AI 剧情知识层，并在初译后执行选择性全局精修。知识和精修调用都按内容缓存，并在 `state.json` 记录实际调用数、缓存命中数、警告数与精修改动数。若只需要确定性分析，可使用 `--no-ai-knowledge`；若不需要精修，可使用 `--no-refine`。这些选项不会影响 RPA/RPYC 采集和基础角色、剧情证据生成。

## 工作区产物

```text
workspace/
├── state.json
├── acquisition.json
├── acquired/
├── decompilation.json
├── decompiled/
├── tools/                    # 仅在按需获取 unrpyc 后生成
├── project-index.json
├── knowledge.json
├── narrative-knowledge.json
├── knowledge-cache/
├── refinement.json
├── refinement-cache/
├── translations/
│   └── scene_<id>.json
├── reports/
│   └── scene_<id>.json
├── build.json
├── install.json              # 仅使用 --install 时生成
└── output/game/tl/<language>/
    ├── <source-script>.rpy
    └── strings.rpy
```

游戏原目录默认只读。所有 AI 结果必须通过结构校验后才能进入构建层；只有明确使用 `--install` 时才会写入 `game/tl/<language>`。安装器允许更新自己生成的文件，但默认拒绝覆盖用户或其他汉化项目已有的同名文件。

## 核心原则

1. 场景是翻译单位，单行只是可写回的最小地址。
2. 世界观、角色和术语由软件自动发现，不是用户的开工表单。
3. 确定性解析优先于模型推理，以降低 Token 消耗。
4. 每个知识结论最终都应带源文件证据和剧情阶段。
5. 游戏源文件不可被翻译流水线直接覆盖。
