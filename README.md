# Clarify Agent

**通过自主追问挖掘用户需求的 RAG 决策规划 Agent。**

用户往往无法一次表达完整需求，直接生成容易得到“看起来完整、实际不合适”的方案。Clarify Agent 以旅游规划为首个验证场景，让 Agent 先确认影响决策的关键条件，再结合资料完成规划。

- **自主追问**：模型根据任务和会话判断是否询问，通过 `ask_user` 等待真实用户答复，继续原任务。
- **澄清后二次检索**：用补充需求重新检索，沿原会话排除末条草稿、补充资料，并在本次终版请求中关闭工具。
- **完整交互链路**：手写 Agent 循环、MCP 网页与文件工具、RAG、多轮记忆、Web/CLI 双入口、流式输出与执行进度。

模型自主决定提问内容与时机。知识库经验不等于用户需求，也不保证票价、时间等信息实时有效。领域迁移需要替换知识库并重新验证。

## 快速开始

需要 **Python 3.11+、uv、Node.js/npm**。克隆本仓库并进入 `clarify-agent` 目录后，在 Windows PowerShell 中执行：

```powershell
uv sync
Copy-Item .env.example .env
```

macOS/Linux 可用 `cp .env.example .env` 创建配置文件。在 `.env` 中填写：

- `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`：兼容 OpenAI 接口的模型配置。
- `EMBEDDING_KEY`、`EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`：向量接口配置。

启动网页：

```powershell
uv run web_main.py
```

浏览器访问 `http://127.0.0.1:8765`，保持终端运行。终端对话使用：

```powershell
uv run main.py
```

`python command.py show` 可查看命令速查表。首次提问会构建当前会话的文档向量库；模型与向量请求会使用你配置的 API 额度。对话记忆保留在本次会话中。

## 评测设计与结果

采用覆盖 **5类需求的20题**评估集，预置首问、隐藏需求和评分要点，建立需求匹配、事实可靠性、交互成本三层指标：

- **A**：使用首轮 RAG，直接生成，不注册询问工具。
- **B**：使用首轮 RAG，自主追问后生成。
- **B2**：复用 B 的完整历史和同一问答轨迹，二次检索、排除末条草稿，并生成终版。

Qwen3.5-Flash 模拟用户逐轮披露需求，设置15轮模拟应答预算及超限收口机制；Qwen-Plus 依逐题预填的评分明细框架判定，代码核算汇总分，保留依据供复核。具体模型可通过环境变量配置。

项目报告记录的20题内部离线对照结果：

- 需求满足率：A **36.4%** → B **76.1%** → B2 **77.5%**。
- 要点覆盖率：A **37.5%** → B **66.3%** → B2 **74.0%**。
- 硬信息准确率：B **50.5%** → B2 **77.0%**。
- 无依据陈述率：B **27.2%** → B2 **17.8%**。

来源：[项目报告](<result/Clarify Agent-项目报告（简历面试版）.md>)。这是固定题库和模型裁判的内部对照，不是线上真实用户 A/B 实验。B/B2 固定交互轨迹，但 B2 同时包含多项终版处理，改善不能单独归因于二次检索。

```powershell
# 跑题 → 裁判，跳过 Excel 录入；会调用配置的模型与向量 API
python command.py eval --skip-record

# 仅跑一题的三种模式
uv run python eval/run_ab.py --tasks T01 --modes A,B,B2

# 只生成裁判提示词，不调用裁判模型
uv run python eval/judge.py --tasks T01 --dry
```

公开仓库保留评估集、脚本、指标说明与汇总报告；不包含完整会话、裁判原文和已填 Excel。Excel 自动录入需自备兼容工作簿，参见 [评估资料说明](result/README.md)。

## 离线验证

```powershell
uv run python -m unittest discover -s tests -v
uv run python data/travel_collection/validate_collection.py
```

自动测试使用模拟模型、向量接口及本地 MCP 测试服务，不调用真实模型 API。知识库校验检查150篇正文与源记录的一致性；公开副本移除了来源链接中的访问签名，保留正文和作者归因。

## 文件入口

- [src/agent.py](src/agent.py)：询问工具、Agent 循环和 B2 终版流程。
- [src/chat_openai.py](src/chat_openai.py)：流式模型请求和消息历史。
- [src/index.py](src/index.py)、[src/web_app.py](src/web_app.py)：终端与网页入口组织。
- [knowledge](knowledge)：50座城市、每城3篇知识文档。
- [eval](eval)：跑题、模拟用户与裁判程序。
- [产品迭代优化](产品迭代优化.md)：两次迭代的问题、方法、验证与边界。
- [部署说明](DEPLOYMENT.md)：Docker 与 Render 部署配置。

运行环境、API 密钥、浏览器状态、缓存和备份不随仓库发布。
