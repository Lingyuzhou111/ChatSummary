# ChatSummary Plugin

**版本:** 1.2
**作者:** Lingyuzhou

聊天记录总结助手 (ChatSummary) 是一个功能强大的插件，用于自动记录、总结和可视化群聊或私聊的对话内容。

## 效果展示
![1745851296403](https://github.com/user-attachments/assets/3d3f219a-cc4f-499b-b90c-acd59d7be414)
![96a8ef576a9246f44376ef7340a8353](https://github.com/user-attachments/assets/5c9c6498-e6de-452d-943f-a6a81be38607)

## 功能特点

-   **自动消息记录**: 保存群聊和私聊的文本消息至 SQLite 数据库 (`chat.db`)。
-   **文本总结**: 生成结构化的文本格式聊天总结，支持按消息数量或时间范围提取。
-   **图片总结**: 将聊天总结渲染为易于分享的图片格式 (HTML -> PNG)。
-   **多模型支持**: 支持多种 LLM API (DeepSeek, ZhiPuAI, SiliconFlow, Qwen 等 OpenAI 兼容接口)。
-   **动态模型切换**: 可在运行时通过命令切换当前使用的 LLM。
-   **GeweChat 集成 (可选)**: 通过 GeweChat API 获取更准确的群聊名称。
-   **自动清理**: 定期清理旧的图片总结输出文件，保持空间整洁。

## 使用方法

### 模型管理

-   **查看可用模型**:
    -   `c打印总结模型`
    -   `c打印模型`
    (会列出在 `config.json` 中已配置 API Key 的模型)
-   **切换当前模型**:
    -   `c切换总结模型 [序号]`
    -   `c切换模型 [序号]`
    (例如: `c切换模型 2`，切换到列表中的第2个模型)

### 聊天记录总结

支持按 **消息数量 (n)** 或 **时间范围 (Xh)** 进行总结。

-   **生成文本总结**:
    -   `c总结` (使用默认条数)
    -   `c总结 50` (总结最近 50 条非命令消息)
    -   `c总结 12h` (总结最近 12 小时内的非命令消息)
-   **生成图片总结**:
    -   `c图片总结` / `图片总结` (使用默认条数)
    -   `c图片总结 100` / `图片总结 100`
    -   `c图片总结 24h` / `图片总结 24h`

**参数说明**:
-   无参数: 使用 `config.json` 中 `default_summary_count` 指定的数量 (当前为 100)。
-   `n`: 总结最近的 `n` 条有效消息 (建议 <= 1000，受 `max_input_tokens` 限制)。
-   `Xh`: 总结最近 `X` 小时内的有效消息 (建议 1 <= X <= 72)。

## 图片总结功能

此功能的基础排版样式源自于 `数字生命-卡兹克 `大佬的公众号文章，在原模板上进行了适当简化。
此功能依赖 `image_summary` 子模块，将大模型生成的 JSON 格式总结渲染为图片。

**依赖安装**:
1.  安装 Python 包: `pip install Jinja2 playwright`
2.  安装 Playwright 浏览器核心: `playwright install`

**工作原理**:
LLM 根据 `image_summary/image_summarize_prompt.txt` 的指示生成 JSON -> 使用 Jinja2 模板渲染成 HTML -> Playwright 截图保存为 PNG 图片。

**注意**: 此功能对 LLM 输出 JSON 的格式有严格要求，若 LLM 返回格式错误、网络不稳定或依赖未正确安装，可能导致生成失败。

## 配置参数 (`config.json`)

| 参数                  | 类型    | 说明                                                                 |
| :-------------------- | :------ | :------------------------------------------------------------------- |
| `default_bot_type`    | string  | 默认使用的 LLM 名称 (必须在 `models` 中已配置 Key)                 |
| `max_input_tokens`    | number  | 允许发送给 LLM 的最大 Token 数量 (Prompt + 消息内容)              |
| `gewechat_api`        | object  | GeweChat API 配置 (用于获取群名)                                   |
| `  enabled`           | boolean | 是否启用 GeweChat API                                                |
| `  base_url`          | string  | GeweChat API 地址                                                    |
| `  appid`             | string  | GeweChat 应用 ID                                                     |
| `  token`             | string  | GeweChat 应用 Token                                                  |
| `models`              | object  | 配置支持的 LLM 模型信息                                              |
| `  "llm_name"`        | object  | (例如 "deepseek", "zhipuai")                                       |
| `    api_base`        | string  | 该模型的 API 端点 URL                                                |
| `    api_key`         | string  | 该模型的 API 密钥                                                    |
| `    model`           | string  | 该模型使用的具体模型标识符                                           |
| `print_model_commands`| array   | 查看可用模型的命令列表                                                 |
| `switch_model_commands`| array  | 切换模型的命令列表                                                   |
| `summarize_commands`  | array   | 文本总结的命令列表                                                   |
| `image_summarize_commands`| array| 图片总结的命令列表                                                   |
| `default_summary_count`| number | 默认总结的消息条数                                                   |
| `summary_prompt`      | string  | **文本总结** 使用的 Prompt 模板 (可包含 `{custom_prompt}` 占位符) |

*(**图片总结** 的 Prompt 在代码内指定路径: `image_summary/image_summarize_prompt.txt`)*

## 数据存储

-   **数据库**: SQLite 文件 `chat.db`
-   **数据表**: `chat_records`
    -   字段: `sessionid`, `msgid`, `user`, `content`, `type`, `timestamp`, `is_triggered`
    -   记录群聊和私聊的文本消息。

## 自动清理

-   **目标**: `image_summary/output/` 目录下的 `.png` 和 `.html` 文件。
-   **条件**: 文件最后修改时间早于 48 小时。
-   **频率**: 每天凌晨 03:00 执行一次 (需要 `schedule` 库支持)。

## 依赖安装

-   **核心**: `requests`
-   **自动清理**: `schedule`
-   **图片总结**: `Jinja2`, `playwright` (还需执行 `playwright install`)

推荐使用 `pip install requests schedule Jinja2 playwright && playwright install` 一次性安装。

## 注意事项

1.  **API Key 配置**: 必须在 `config.json` 的 `models` 部分为至少一个模型配置有效的 `api_key` 才能使用总结功能。
2.  **图片总结依赖**: 确保已正确安装 Jinja2, Playwright 并执行 `playwright install`。
3.  **GeweChat API**: 此为可选功能，用于改善图片总结中的群聊名称显示。若禁用或配置错误，将默认显示 "本群"。
4.  **消息过滤**: 插件在总结时会自动过滤掉触发总结的命令消息本身。
5.  **Token 限制**: 注意 `max_input_tokens` 配置，过长的聊天记录可能被截断或导致 API 调用失败。

## 错误处理

-   **"总结失败：API 错误..."**:
    -   检查 `config.json` 中对应模型的 `api_base` 和 `api_key` 是否正确。
    -   确认网络连接是否通畅，能否访问 API 地址。
    -   检查 LLM 账户余额或额度是否充足。
    -   检查 `max_input_tokens` 是否设置过小。
-   **"图片总结失败..." / "图片总结功能未启用..."**:
    -   确认已执行 `pip install Jinja2 playwright && playwright install`。
    -   查看插件启动日志，确认 `image_summarize` 模块是否加载成功。
    -   查看运行时日志，检查 LLM 是否返回了有效的 JSON 数据 (图片总结依赖此数据)。
    -   尝试减少总结的消息数量或时间范围。
-   **"切换模型失败..."**:
    -   确认目标模型的 API Key 是否在 `config.json` 中配置且有效。
