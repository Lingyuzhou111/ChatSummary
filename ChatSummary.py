# encoding:utf-8

import json
import os
import time
import sqlite3
import requests
from urllib.parse import urlparse
import hmac
import base64
import time
import json
from urllib.parse import urlparse
from pathlib import Path
import io
import threading
import schedule

import plugins
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from plugins import Event, EventContext, EventAction
from channel.chat_channel import check_contain, check_prefix
from channel.chat_message import ChatMessage
from common.log import logger
from plugins import *


@plugins.register(
    name="ChatSummary",
    desire_priority=99,
    hidden=False,
    enabled=True,
    desc="聊天记录总结助手(支持图片和自动清理)",
    version="1.2",
    author="lanvent",
)
class ChatSummary(Plugin):
   
    max_tokens = 4000
    max_input_tokens = 8000  # 默认限制输入 8000 个 token
    prompt = '''你是一个专业的群聊记录总结助手，请按照以下规则和格式对群聊内容进行总结：

规则要求：
1. 总结层次分明，突出重点：
   - 提取重要信息和核心讨论要点
   - 突出关键词、数据、观点和结论
   - 保持内容完整，避免过度简化
2. 多话题处理：
   - 按主题分类整理
   - 相关话题可以适当合并
   - 保持时间顺序
3. 关注重点：
   - 突出重要发言人的观点
   - 弱化非关键对话内容
   - 标注重要结论和待办事项

输出格式：
1️⃣ [话题1]🔥🔥
• 时间：MM-DD HH:mm - HH:mm
• 参与者：
• 核心内容：
• 重要结论：
• 待办事项：（如果有）

2️⃣ [话题2]🔥
...

注意事项：
- 话题标题控制在50字以内
- 使用1️⃣2️⃣3️⃣作为话题序号
- 用🔥数量表示话题热度（1-3个）
- [x]表示emoji或媒体文件说明
- 带<T>的消息为机器人触发，可降低权重
- 带#和$的消息为插件触发，可忽略

用户特定指令：{custom_prompt}'''
    image_summary_prompt_path = Path(__file__).parent / "image_summary" / "image_summarize_prompt.txt" # 图片总结的 Prompt 路径

    def __init__(self):
        super().__init__()
        self.image_summarize_module = None
        self.image_summarize_enabled = False
        self.cleanup_target_dir = Path(__file__).parent / "image_summary" / "output"
        self.group_name_cache = {} # Initialize group name cache

        try:
            curdir = Path(__file__).parent
            config_path = curdir / "config.json"
            self.config = self._load_config(config_path)

            # Load global config
            self.max_tokens = self.config.get("max_tokens", self.max_tokens)
            self.max_input_tokens = self.config.get("max_input_tokens", self.max_input_tokens)
            self.prompt = self.config.get("summary_prompt", self.prompt)
            
            # Load commands
            self.print_commands = self.config.get('print_model_commands', ["c打印总结模型", "c打印模型"])
            self.switch_commands = self.config.get('switch_model_commands', ["c切换总结模型", "c切换模型"])
            self.summarize_commands = self.config.get('summarize_commands', ["c总结"])
            self.image_summarize_commands = self.config.get('image_summarize_commands', ["c图片总结"])
            self.default_summary_count = self.config.get('default_summary_count', 100)

            # Load model config
            self.bot_type = self.config.get('default_bot_type', 'zhipuai')
            self.models_config = self.config.get('models', {})

            for bot_type, config in self.models_config.items():
                if not config.get('api_key'):
                    logger.warning(f"[ChatSummary] 文本模型 {bot_type} API 密钥未在配置中找到")

            if self.bot_type in self.models_config:
                 self._set_current_model_config()
            else:
                 logger.error(f"[ChatSummary] 未找到默认文本模型 {self.bot_type} 的配置，请检查 config.json")

            # +++ Load GeweChat API Config +++
            gewechat_config = self.config.get("gewechat_api", {})
            self.gewechat_enabled = gewechat_config.get("enabled", False)
            self.gewechat_base_url = gewechat_config.get("base_url", "")
            self.gewechat_appid = gewechat_config.get("appid", "")
            self.gewechat_token = gewechat_config.get("token", "")

            if self.gewechat_enabled:
                if not all([self.gewechat_base_url, self.gewechat_appid, self.gewechat_token]):
                    logger.warning("[ChatSummary] GeweChat API is enabled but configuration (base_url, appid, token) is incomplete. Disabling feature.")
                    self.gewechat_enabled = False
                else:
                    logger.info("[ChatSummary] GeweChat API for group name fetching is enabled.")
            else:
                logger.info("[ChatSummary] GeweChat API for group name fetching is disabled.")

            # Image summary module import
            try:
                from .image_summary import image_summarize
                self.image_summarize_module = image_summarize
                try:
                    self.image_summarize_module.check_dependencies()
                    self.image_summarize_enabled = True
                    logger.info("[ChatSummary] image_summarize 模块及其依赖加载成功.")
                except ImportError as dep_error:
                    logger.error(f"[ChatSummary] image_summarize 依赖检查失败: {dep_error}")
                    logger.error("[ChatSummary] 图片总结功能将不可用。请安装: pip install Jinja2 playwright && playwright install")
                    self.image_summarize_enabled = False
                    self.image_summarize_module = None
            except ImportError as import_err:
                logger.error(f"[ChatSummary] 无法导入 image_summarize 模块: {import_err}")
                logger.error("[ChatSummary] 图片总结功能将不可用。")
                self.image_summarize_enabled = False

            # Init DB
            db_path = curdir / "chat.db"
            self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
            self._init_database()

            # Check image summary prompt
            if not self.image_summary_prompt_path.is_file():
                logger.error(f"[ChatSummary] 图片总结 Prompt 文件未找到: {self.image_summary_prompt_path}")
                self.image_summarize_enabled = False
                logger.warning("[ChatSummary] 图片总结功能因缺少 Prompt 文件而被禁用。")

            # Register handlers
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
            self.handlers[Event.ON_RECEIVE_MESSAGE] = self.on_receive_message
            logger.info("[ChatSummary] 初始化完成，当前文本模型: %s", self.bot_type)
            if self.image_summarize_enabled:
                logger.info("[ChatSummary] 图片总结功能已启用.")
            else:
                logger.warning("[ChatSummary] 图片总结功能已禁用 (原因见上方日志)." )

            # Background cleanup task
            try:
                import schedule
                logger.info("[ChatSummary] Starting background cleanup scheduler...")
                self.scheduler_thread = threading.Thread(target=self._run_scheduler, daemon=True)
                self.scheduler_thread.start()
                logger.info(f"[ChatSummary] Cleanup scheduler started for directory: {self.cleanup_target_dir}")
            except ImportError:
                logger.warning("[ChatSummary] 'schedule' library not found. Automatic cleanup disabled.")
                logger.warning("[ChatSummary] Please install it using: pip install schedule")
            except Exception as scheduler_e:
                logger.error(f"[ChatSummary] Failed to start cleanup scheduler: {scheduler_e}", exc_info=True)

        except Exception as e:
            logger.error(f"[ChatSummary] 初始化失败: {e}", exc_info=True)
            raise e

    def _set_current_model_config(self):
        """设置当前模型的配置"""
        if self.bot_type not in self.models_config:
            logger.error(f"[ChatSummary] 未找到模型 {self.bot_type} 的配置")
            raise Exception(f"未找到模型 {self.bot_type} 的配置")

        current_config = self.models_config[self.bot_type]
        self.api_base = current_config.get('api_base', '')
        self.api_key = current_config.get('api_key', '')
        self.model = current_config.get('model', '')

        if not self.api_key:
            logger.error(f"[ChatSummary] {self.bot_type} API 密钥未配置")
            raise Exception("API 密钥未配置")

    def _load_config(self, config_path):
        """从 config.json 加载配置"""
        try:
            if not os.path.exists(config_path):
                # 如果配置文件不存在，返回一个包含默认 group_name_mapping 的空配置
                return {"group_name_mapping": {}}
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
                # 确保 group_name_mapping 存在，如果不存在则添加空字典
                if "group_name_mapping" not in config_data:
                    config_data["group_name_mapping"] = {}
                return config_data
        except json.JSONDecodeError as e:
            logger.error(f"[ChatSummary] 解析配置文件失败 ({config_path}): {e}")
            # 解析失败也返回带默认 group_name_mapping 的空配置
            return {"group_name_mapping": {}} 
        except Exception as e:
            logger.error(f"[ChatSummary] 加载配置失败: {e}")
            return {"group_name_mapping": {}}

    def _save_config(self):
        """保存当前配置（特别是 default_bot_type）到 config.json"""
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        try:
            # 确保 self.config 包含最新的 default_bot_type
            # 注意：这里直接写入 self.config，如果其他地方修改了 self.config 的其他部分也会被保存
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=4)
            logger.info(f"[ChatSummary] 配置已保存到 {config_path}，默认模型: {self.config.get('default_bot_type')}")
        except Exception as e:
            logger.error(f"[ChatSummary] 保存配置失败: {e}")
            # 保存失败不应阻止程序运行，但要记录错误

    def _prepare_api_request(self, content):
        """根据当前 bot_type 准备 API 请求的 headers 和 payload"""
        headers = {'Content-Type': 'application/json'}
        messages = [{"role": "user", "content": content}]
        payload = {}

        if self.bot_type == 'zhipuai':
            # 智谱 API 特殊处理
            try:


                api_key_list = self.api_key.split('.')
                if len(api_key_list) != 2:
                    raise ValueError("Invalid API key format for zhipuai")

                key_id, key = api_key_list
                now = int(time.time())
                exp = now + 3600  # 1小时后过期

                jwt_payload = {"api_key": key_id, "exp": exp, "timestamp": now}
                jwt_header = {"alg": "HS256", "sign_type": "SIGN"}

                header_str = base64.b64encode(json.dumps(jwt_header).encode('utf-8')).decode('utf-8').replace('=', '')
                payload_str = base64.b64encode(json.dumps(jwt_payload).encode('utf-8')).decode('utf-8').replace('=', '')

                signature = hmac.new(
                    key.encode('utf-8'),
                    f"{header_str}.{payload_str}".encode('utf-8'),
                    'sha256'
                ).digest()

                jwt_token = f"{header_str}.{payload_str}.{base64.b64encode(signature).decode('utf-8').replace('=', '')}"
                headers['Authorization'] = f"Bearer {jwt_token}"

                payload = {
                    'model': self.model,
                    'messages': messages,
                    'stream': False,
                    'temperature': 0.7,
                    'top_p': 0.7,
                    'max_tokens': self.max_tokens,
                    'tools': [],
                    'request_id': f'summary_{int(time.time())}'
                }
            except Exception as e:
                logger.error(f"[ChatSummary] 生成智谱 API Token 失败: {e}")
                raise ValueError(f"生成智谱 API Token 失败: {e}")

        elif self.bot_type == 'deepseek':
            headers['Authorization'] = f"Bearer {self.api_key}"
            headers['Host'] = urlparse(self.api_base).netloc
            payload = {
                'model': self.model,
                'messages': messages,
                'max_tokens': self.max_tokens
            }
        elif self.bot_type == 'siliconflow':
             headers['Authorization'] = f"Bearer {self.api_key}"

             payload = {
                 'model': self.model,
                 'messages': messages,
                 'max_tokens': self.max_tokens
             }
        else:
             # 默认使用 OpenAI 兼容格式
             headers['Authorization'] = f"Bearer {self.api_key}"
             payload = {
                 'model': self.model,
                 'messages': messages,
                 'max_tokens': self.max_tokens
             }

        # 确保返回三个值
        return headers, payload, self.api_base # 返回 api_base

    def _insert_record(self, session_id, msg_id, user, content, msg_type, timestamp, is_triggered = 0):
        """将记录插入到数据库"""
        try:
            c = self.conn.cursor()
            logger.debug(f"[ChatSummary] Attempting to insert record: sessionid={session_id}, msgid={msg_id}, user={user}, content_len={len(content) if content else 0}, type={msg_type}, ts={timestamp}, triggered={is_triggered}")
            c.execute("INSERT OR REPLACE INTO chat_records VALUES (?,?,?,?,?,?,?)",
                      (str(session_id), int(msg_id), str(user), str(content), str(msg_type), int(timestamp), int(is_triggered)))
            self.conn.commit()
            logger.debug(f"[ChatSummary] Record inserted successfully for session {session_id}, msgid {msg_id}")
        except sqlite3.Error as e:
            logger.error(f"[ChatSummary] Database error occurred during insert: {e} | Data: session={session_id}, msg={msg_id}, user={user}, content={content[:50]}..., type={msg_type}, ts={timestamp}, trig={is_triggered}")
        except Exception as e:
            logger.error(f"[ChatSummary] Unexpected error during insert: {e}", exc_info=True)

    def _get_records(self, session_id, start_timestamp=0, limit=9999):
        """从数据库获取记录 (只获取文本类型)"""
        c = self.conn.cursor()
        target_type = str(ContextType.TEXT)
        c.execute("SELECT * FROM chat_records WHERE sessionid=? and timestamp>? AND type=? ORDER BY timestamp DESC LIMIT ?",
                  (session_id, start_timestamp, target_type, limit))
        return c.fetchall()

    def on_receive_message(self, e_context: EventContext):
        """处理接收到的消息，存储到数据库"""
        logger.debug("[ChatSummary] on_receive_message called.")
        context = e_context['context']
        cmsg : ChatMessage = e_context['context']['msg']

        if context.type != ContextType.TEXT:
             logger.debug(f"[ChatSummary] Skipping non-text message (type: {context.type})")
             return

        content = context.content
        if not content or (isinstance(content, str) and content.strip().startswith('<?xml')):
            logger.debug("[ChatSummary] Skipping empty or XML message.")
            return

        session_id = None
        username = None
        if context.get("isgroup", False):
            session_id = cmsg.other_user_id
            if not session_id:
                logger.warning("[ChatSummary] Group chat message missing other_user_id, cannot determine session_id. Skipping record.")
                return
            username = cmsg.actual_user_nickname or cmsg.actual_user_id
        else:
            session_id = cmsg.from_user_id
            username = cmsg.from_user_nickname or cmsg.from_user_id

        if not session_id or not username:
             logger.warning(f"[ChatSummary] Could not determine session_id ({session_id}) or username ({username}). Skipping.")
             return

        is_triggered = False
        is_plugin_command = False
        all_commands = self.print_commands + self.switch_commands + self.summarize_commands + self.image_summarize_commands
        for cmd in all_commands:
            if content == cmd or content.startswith(cmd + " "):
                is_plugin_command = True
                break
        if is_plugin_command:
            is_triggered = True

        if not is_triggered and context.get("isgroup", False):
            match_contain = check_contain(content, self.config.get('group_chat_keyword'))
            if match_contain is not None:
                is_triggered = True
            if not is_triggered and context['msg'].is_at and not self.config.get("group_at_off", False):
                is_triggered = True

        content_to_store = str(content)
        self._insert_record(session_id, cmsg.msg_id, username, content_to_store, str(context.type), cmsg.create_time, int(is_triggered))

    def on_handle_context(self, e_context: EventContext):
        """处理消息命令: 文本总结、图片总结、模型管理"""
        context = e_context['context']
        content = context.content.strip()
        logger.debug(f"[ChatSummary] on_handle_context. content: '{content}'")

        command_found = False
        args = []
        command_type = None
        summary_type = None

        if content in self.print_commands:
            command_found = True
            command_type = "print"
            args = []
        else:
            for cmd in self.summarize_commands:
                if content == cmd or content.startswith(cmd + " "):
                    command_found = True
                    command_type = "summarize"
                    remaining = content[len(cmd):].strip()
                    summary_type, args = self._parse_summary_args(remaining)
                    break
            if not command_found:
                for cmd in self.image_summarize_commands:
                    if content == cmd or content.startswith(cmd + " "):
                        command_found = True
                        command_type = "image_summary"
                        remaining = content[len(cmd):].strip()
                        summary_type, args = self._parse_summary_args(remaining)
                        break
            if not command_found:
                for cmd in self.switch_commands:
                     if content.startswith(cmd + " "):
                         command_found = True
                         command_type = "switch"
                         args = content[len(cmd):].strip().split()
                         break

        if command_found:
            logger.info(f"[ChatSummary] Matched command: {command_type}, args: {args}, summary_type: {summary_type}")
            reply = None
            action = EventAction.BREAK_PASS

            if command_type == "summarize":
                reply_content = self._handle_summarize(args, e_context, summary_type)
                logger.debug(f"[ChatSummary] _handle_summarize returned: '{reply_content[:100]}...' (type: {type(reply_content)})")
                if reply_content:
                    reply = Reply(ReplyType.TEXT, reply_content)
            elif command_type == "image_summary":
                self._handle_text_summary_to_image(args, e_context, summary_type)
                return
            elif command_type == "print" or command_type == "switch":
                reply_content = self._handle_model_command(args, e_context, command_type)
                if reply_content:
                    reply = Reply(ReplyType.TEXT, reply_content)
            else:
                 logger.warning(f"[ChatSummary] Unknown command type: {command_type}")

            if reply:
                logger.info(f"[ChatSummary] Reply object created for command '{command_type}'. Setting action to BREAK_PASS.")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS # 命令匹配成功，阻止后续插件处理
                return
            else:
                logger.warning(f"[ChatSummary] No reply object created for command '{command_type}'. Current action: {e_context.action}")
                if command_type == "image_summary" and e_context.action != EventAction.BREAK_PASS:
                    e_context.action = EventAction.BREAK_PASS
                    logger.info(f"[ChatSummary] Forcing action to BREAK_PASS for failed image_summary command.")
                logger.info(f"[ChatSummary] No reply for '{command_type}', final action will be: {e_context.action}")
                pass # 保持 action 为 BREAK_PASS 或 BREAK_PASS (由内部函数决定)

        else:
            logger.debug(f"[ChatSummary] 未匹配到任何已知命令: '{content}'")
            e_context.action = EventAction.CONTINUE

    def _parse_summary_args(self, remaining: str) -> tuple[str, list[str]]:
        """解析总结命令的参数 (数量或时间)"""
        if not remaining:
            return "time", ["24"]
        elif remaining.lower().endswith('h'):
            try:
                hours = int(remaining[:-1])
                if 1 <= hours <= 72:
                    return "time", [str(hours)]
                else:
                    logger.warning(f"[ChatSummary] Invalid hour range: {hours}. Using default count.")
                    return "count", [str(self.default_summary_count)]
            except ValueError:
                logger.warning(f"[ChatSummary] Invalid hour format: {remaining}. Using default count.")
                return "count", [str(self.default_summary_count)]
        else:
            try:
                count = int(remaining)
                if count <= 0:
                     logger.warning(f"[ChatSummary] Invalid count: {count}. Using default count.")
                     return "count", [str(self.default_summary_count)]
                elif count > 1000:
                     logger.warning(f"[ChatSummary] Requested count {count} too large, limiting to 1000.")
                     return "count", ["1000"]
                else:
                     return "count", [str(count)]
            except ValueError:
                logger.warning(f"[ChatSummary] Invalid count format: {remaining}. Using default count.")
                return "count", [str(self.default_summary_count)]

    def _handle_model_command(self, args, e_context: EventContext, command_type: str):
        """处理文本模型相关命令（打印和切换）"""
        # 获取在 config.json 中配置且有 API Key 的有效模型
        available_bot_types = []
        model_info = {}
        for bot_type, config in self.models_config.items():
            if config.get('api_key'): # 只显示配置了 key 的文本模型
                available_bot_types.append(bot_type)
                model_info[bot_type] = config.get('model', '未知模型名')

        if not available_bot_types:
            return "配置文件中没有找到任何已配置API Key的可用文本模型。"

        if command_type == "print" or not args:
            reply_text = "ChatSummary可用文本模型：\n"
            for i, bot_type in enumerate(available_bot_types, 1):
                prefix = "👉" if bot_type == self.bot_type else "  "
                reply_text += f"{prefix}{i}. {bot_type} ({model_info.get(bot_type, '?')})\n" # 使用 get 避免 keyerror
            return reply_text.rstrip()

        # 切换命令
        try:
            model_index = int(args[0]) - 1
            if 0 <= model_index < len(available_bot_types):
                target_bot_type = available_bot_types[model_index]
                if target_bot_type == self.bot_type:
                    return f"已经是 {target_bot_type} ({model_info.get(target_bot_type, '?')}) 文本模型了，无需切换。"
                try:
                    old_bot_type = self.bot_type
                    self.bot_type = target_bot_type
                    self._set_current_model_config() # 会验证新模型的 key
                    self.config['default_bot_type'] = target_bot_type
                    self._save_config()
                    return f"✅已切换文本总结模型: {target_bot_type} ({model_info.get(target_bot_type, '?')})"
                except Exception as e:
                    logger.error(f"[ChatSummary] 切换到文本模型 {target_bot_type} 失败: {e}")
                    self.bot_type = old_bot_type # 切换失败，恢复原状
                    self._set_current_model_config() # 确保配置一致
                    return f"切换文本模型失败: {e}"
            else:
                return f"无效的模型序号。请输入 1 到 {len(available_bot_types)} 之间的数字。"
        except ValueError:
            return "无效的参数。请提供要切换的文本模型序号（数字）。"
        except Exception as e:
            logger.error(f"[ChatSummary] 处理模型命令时发生错误: {e}", exc_info=True)
            return f"处理命令时发生错误: {e}"

    def _handle_summarize(self, args, e_context: EventContext, summary_type="count"):
        """处理文本总结命令"""
        try:
            msg = e_context['context']['msg']
            session_id = msg.from_user_id
            if e_context['context'].get("isgroup", False) and msg.other_user_id:
                session_id = msg.other_user_id
            elif not session_id:
                logger.error("[ChatSummary] 无法确定 session_id")
                return "无法确定会话ID，无法进行总结。"

            messages, actual_count = "", 0
            time_info = ""

            if summary_type == "time":
                hours = int(args[0])
                start_timestamp = time.time() - (hours * 3600)
                messages, actual_count = self._get_chat_messages_by_time(session_id, start_timestamp)
                time_info = f"过去{hours}小时内"
            else: # count
                requested_count = int(args[0])
                messages, actual_count = self._get_chat_messages_by_count(session_id, requested_count)
                time_info = f"最近{actual_count}"

            if not messages:
                return f"在{time_info}没有找到可总结的消息。"

            # 使用文本总结的 Prompt
            final_prompt = self.prompt.format(
                custom_prompt=f"本次总结的是{time_info}的 {actual_count} 条消息。"
            )

            # 检查 Token 数量
            estimated_input_tokens = len(final_prompt) + len(messages) # 粗略估计
            if estimated_input_tokens > self.max_input_tokens:
                available_tokens = self.max_input_tokens - len(final_prompt) - 100 # 保留余量
                messages = messages[-available_tokens:] # 简单截断尾部
                logger.warning(f"[ChatSummary] Input tokens exceeded limit, truncating messages to fit {self.max_input_tokens} tokens.")

            # 生成总结
            full_content_for_llm = final_prompt + "\n\n以下是需要总结的群聊内容：\n" + messages
            summary = self._call_llm_api(full_content_for_llm)
            logger.debug(f"[ChatSummary] _call_llm_api returned for text summary: '{summary[:100]}...' (type: {type(summary)})")
            return summary

        except Exception as e:
            logger.error(f"[ChatSummary] 文本总结消息失败: {e}", exc_info=True)
            # 返回错误信息字符串，确保非空
            return f"文本总结失败: {e}"

    def _get_chat_messages_by_time(self, session_id, start_timestamp):
        """按时间范围获取文本聊天记录"""
        try:
            records = self._get_records(session_id, start_timestamp, 1000) # Limit 足够大
            if not records:
                return None, 0

            formatted_messages = []
            actual_msg_count = 0
            processed_msg_ids = set() # 避免重复记录 (虽然主键应该保证唯一性)

            for record in reversed(records): # 从旧到新
                msg_id = record[1]
                if msg_id in processed_msg_ids: continue
                processed_msg_ids.add(msg_id)

                content = record[3]
                user = record[2] or "未知用户"
                timestamp = record[5]
                is_triggered = record[6] # 0 or 1

                # 只添加非触发、非空的内容
                if content and not is_triggered:
                    time_str = time.strftime("%m-%d %H:%M", time.localtime(timestamp))
                    # <T> 标记逻辑可以保留或移除，取决于 Prompt 是否需要
                    user_marker = "<T>" if user.lower() in ["system", "admin"] else ""
                    formatted_messages.append(f"{user_marker}[{time_str}] {user}: {content.strip()}")
                    actual_msg_count += 1

            if not formatted_messages:
                return None, 0

            return "\n".join(formatted_messages), actual_msg_count
        except Exception as e:
            logger.error(f"[ChatSummary] 获取时间范围消息失败: {e}")
            return None, 0

    def _get_chat_messages_by_count(self, session_id, msg_count):
        """按消息数量获取文本聊天记录"""
        try:
            # 稍微多获取一些记录，因为需要跳过触发消息
            limit = int(msg_count * 1.5) + 10 # 增加获取量
            # 获取最近 N 天可能不够，直接按 limit 获取最新的
            records = self._get_records(session_id, 0, limit) # start_timestamp=0 获取所有

            if not records:
                return None, 0

            formatted_messages = []
            actual_msg_count = 0
            processed_msg_ids = set()

            # records 是按 timestamp DESC 排序的，所以直接遍历就是从新到旧
            for record in records:
                if actual_msg_count >= msg_count: # 达到目标数量就停止
                     break

                msg_id = record[1]
                if msg_id in processed_msg_ids: continue
                processed_msg_ids.add(msg_id)

                content = record[3]
                user = record[2] or "未知用户"
                timestamp = record[5]
                is_triggered = record[6]

                # 只添加非触发、非空的内容
                if content and not is_triggered:
                    time_str = time.strftime("%m-%d %H:%M", time.localtime(timestamp))
                    user_marker = "<T>" if user.lower() in ["system", "admin"] else ""
                    # 插入到列表开头，保持时间从旧到新
                    formatted_messages.insert(0, f"{user_marker}[{time_str}] {user}: {content.strip()}")
                    actual_msg_count += 1

            if not formatted_messages:
                return None, 0

            return "\n".join(formatted_messages), actual_msg_count
        except Exception as e:
            logger.error(f"[ChatSummary] 获取指定数量消息失败: {e}")
            return None, 0

    def _call_llm_api(self, prompt):
        """调用文本 LLM API 生成总结 (包括处理 JSON 的情况)"""
        try:
            headers, payload, api_base = self._prepare_api_request(prompt)

            # 确定 API URL
            url = api_base
            if not url:
                 logger.error(f"[ChatSummary] API base URL 未配置 for bot type {self.bot_type}")
                 return f"总结失败：API基础URL未配置"

            logger.debug(f"[ChatSummary] Calling LLM API URL: {url}")
            # logger.debug(f"[ChatSummary] Payload: {json.dumps(payload, indent=2, ensure_ascii=False)}") # Debug payload

            response = requests.post(url, headers=headers, json=payload, timeout=180)

            if response.status_code == 200:
                result = response.json()
                # logger.debug(f"[ChatSummary] LLM API Response: {result}") # Debug response
                summary = ""
                # 不同 API 返回结构适配
                if self.bot_type == 'zhipuai':
                    try:
                        summary = result['choices'][0]['message']['content'].strip()
                    except (KeyError, IndexError, TypeError) as e:
                         logger.error(f"[ChatSummary] 解析 Zhipu 响应失败: {e}, 响应: {result}")
                         return "总结失败：无法解析 Zhipu API 响应"
                else: # 默认 OpenAI / Siliconflow / Qwen 兼容
                    try:
                        summary = result['choices'][0]['message']['content'].strip()
                    except (KeyError, IndexError, TypeError) as e:
                         logger.error(f"[ChatSummary] 解析 OpenAI/兼容 响应失败: {e}, 响应: {result}")
                         return "总结失败：无法解析 API 响应"

                # 返回原始文本，让调用者处理 JSON 解析或后处理
                return summary
            else:
                error_text = f"API 错误 ({response.status_code}): {response.text[:200]}..."
                logger.error(f"[ChatSummary] {error_text}")
                # 返回包含错误信息的文本
                if "insufficient_quota" in response.text.lower():
                    return f"总结失败：API 错误 {response.status_code} (余额不足或额度用尽)"
                elif "invalid_api_key" in response.text.lower() or "API key is invalid" in response.text:
                     return f"总结失败：API 错误 {response.status_code} (API Key 无效)"
                elif response.status_code == 400 and "maximum context length" in response.text:
                     return f"总结失败：API 错误 {response.status_code} (输入内容过长，已超出模型限制)"
                else:
                    return f"总结失败：{error_text}"

        except requests.exceptions.Timeout:
            logger.error(f"[ChatSummary] API 请求超时")
            return "总结失败：请求超时"
        except ValueError as e: # 捕获 _prepare_api_request 或其他地方的 ValueError
             logger.error(f"[ChatSummary] 值错误: {e}")
             return f"总结失败：配置或参数错误 ({e})"
        except Exception as e:
            logger.error(f"[ChatSummary] 总结生成时发生未知错误: {e}", exc_info=True)
            return f"总结失败：内部错误 ({e})"

    def _init_database(self):
        """初始化数据库架构"""
        c = self.conn.cursor()
        # 增加索引提升查询效率
        c.execute("""CREATE TABLE IF NOT EXISTS chat_records
                    (sessionid TEXT NOT NULL,
                     msgid INTEGER NOT NULL,
                     user TEXT,
                     content TEXT,
                     type TEXT,
                     timestamp INTEGER,
                     is_triggered INTEGER DEFAULT 0,
                     PRIMARY KEY (sessionid, msgid))""")
        # 检查并添加索引（如果不存在）
        indices = c.execute("PRAGMA index_list(chat_records)").fetchall()
        index_names = [idx[1] for idx in indices]
        if 'idx_chat_records_session_ts_type' not in index_names:
             c.execute("CREATE INDEX idx_chat_records_session_ts_type ON chat_records (sessionid, timestamp DESC, type)")
             logger.info("[ChatSummary] Created index idx_chat_records_session_ts_type on chat_records table.")

        # 检查 is_triggered 列是否存在 (保持原有逻辑)
        c = c.execute("PRAGMA table_info(chat_records);")
        column_exists = any(column[1] == 'is_triggered' for column in c.fetchall())
        if not column_exists:
            self.conn.execute("ALTER TABLE chat_records ADD COLUMN is_triggered INTEGER DEFAULT 0;")
            self.conn.execute("UPDATE chat_records SET is_triggered = 0;")
            logger.info("[ChatSummary] Added is_triggered column to chat_records table.")

        self.conn.commit()

    def get_help_text(self, verbose=False, **kwargs):
        """获取插件帮助信息 (更新)"""
        help_text = f"""🤖 微信群聊总结助手 v{self.version}

支持的命令：
1. 模型管理 (文本总结模型)
   - 查看可用模型：{'、'.join(f'`{cmd}`' for cmd in self.print_commands)}
   - 切换模型：{'、'.join(f'`{cmd} [序号]`' for cmd in self.switch_commands)} (例如: c切换模型 2)

2. 聊天记录总结
   - 生成文本总结：{'、'.join(f'`{cmd}`' for cmd in self.summarize_commands)}
   - 生成图片总结：{'、'.join(f'`{cmd}`' for cmd in self.image_summarize_commands)}

   总结参数 (对文本和图片总结都有效)：
   - `[命令]` : 总结最近 {self.default_summary_count} 条消息 (默认)
   - `[命令] n` : 总结最近 n 条消息 (如: c总结 50)
   - `[命令] Xh` : 总结最近 X 小时内的消息 (如: c图片总结 12h)

注意事项：
1. 首次使用需在 `config.json` 中配置模型的 API 密钥。
2. 支持的文本 API 接口：deepseek、zhipuai、siliconflow、qwen。
3. 图片总结功能需要额外安装依赖：`pip install Jinja2 playwright && playwright install`。
4. 图片总结依赖大模型输出特定 JSON 格式，如果模型不支持或网络不稳定可能失败。
5. 总结时会过滤掉命令消息和机器人消息。
6. 时间范围总结限制在 1-72 小时内。
7. 消息数量总结限制在 1000 条内。
"""
        return help_text

    def _handle_text_summary_to_image(self, args, e_context: EventContext, summary_type="count"):
        """处理将文本总结渲染为图片的命令"""
        # 使用 self 属性进行检查
        if not self.image_summarize_enabled or self.image_summarize_module is None:
            e_context["reply"] = Reply(ReplyType.TEXT, "图片总结功能未启用或初始化失败。请检查日志并确保已安装依赖 (Jinja2, Playwright)。")
            e_context.action = EventAction.BREAK_PASS # 修改为 BREAK_PASS
            logger.info(f"[ChatSummary] Image summary disabled, setting action to BREAK_PASS") # 更新日志
            return

        try:
            msg = e_context['context']['msg']
            session_id = msg.from_user_id
            if e_context['context'].get("isgroup", False) and msg.other_user_id:
                session_id = msg.other_user_id
            elif not session_id:
                 raise ValueError("无法确定会话ID")

            # 1. 获取文本消息
            messages, actual_count = "", 0
            time_info = ""
            if summary_type == "time":
                hours = int(args[0])
                start_timestamp = time.time() - (hours * 3600)
                messages, actual_count = self._get_chat_messages_by_time(session_id, start_timestamp)
                time_info = f"过去{hours}小时内"
            else: # count
                requested_count = int(args[0])
                messages, actual_count = self._get_chat_messages_by_count(session_id, requested_count)
                time_info = f"最近{actual_count}"

            if not messages:
                reply_content = f"在{time_info}没有找到可总结的消息。"
                e_context["reply"] = Reply(ReplyType.TEXT, reply_content)
                e_context.action = EventAction.BREAK_PASS # 修改为 BREAK_PASS
                logger.info(f"[ChatSummary] No messages found, setting action to BREAK_PASS") # 更新日志
                return

            # 2. 加载并格式化 Prompt (使用图片总结的 Prompt)
            try:
                with open(self.image_summary_prompt_path, 'r', encoding='utf-8') as f:
                    image_prompt_template = f.read()
            except FileNotFoundError:
                 logger.error(f"图片总结 Prompt 文件未找到: {self.image_summary_prompt_path}")
                 reply_content = "图片总结失败：缺少 Prompt 文件。"
                 e_context["reply"] = Reply(ReplyType.TEXT, reply_content)
                 e_context.action = EventAction.BREAK_PASS # 修改为 BREAK_PASS
                 logger.info(f"[ChatSummary] Prompt file not found, setting action to BREAK_PASS") # 更新日志
                 return
            except Exception as e:
                 logger.error(f"读取图片总结 Prompt 文件失败: {e}")
                 reply_content = "图片总结失败：读取 Prompt 文件出错。"
                 e_context["reply"] = Reply(ReplyType.TEXT, reply_content)
                 e_context.action = EventAction.BREAK_PASS # 修改为 BREAK_PASS
                 logger.info(f"[ChatSummary] Error reading prompt file, setting action to BREAK_PASS") # 更新日志
                 return

            # 填充消息记录和可能的元信息到 Prompt
            formatted_prompt = image_prompt_template + f"\n\n--- 待总结的聊天记录 ({time_info} {actual_count}条) ---\n" + messages

            # 3. 调用 LLM API 获取 JSON 响应
            logger.info("[ChatSummary] Requesting JSON summary from LLM...")
            llm_response_text = self._call_llm_api(formatted_prompt)

            # 检查 LLM 是否返回了错误信息
            if llm_response_text.startswith("总结失败："):
                e_context["reply"] = Reply(ReplyType.TEXT, llm_response_text)
                e_context.action = EventAction.BREAK_PASS # 修改为 BREAK_PASS
                logger.info(f"[ChatSummary] LLM API call failed, setting action to BREAK_PASS") # 更新日志
                return

            # 4. 解析 JSON
            summary_data = None
            try:
                cleaned_response = llm_response_text.strip()
                if cleaned_response.startswith("```json"):
                    cleaned_response = cleaned_response[7:]
                if cleaned_response.endswith("```"):
                    cleaned_response = cleaned_response[:-3]
                summary_data = json.loads(cleaned_response.strip())
                if not isinstance(summary_data, dict):
                     raise ValueError("LLM did not return a JSON object.")
                logger.info("[ChatSummary] Successfully parsed JSON summary from LLM.")
                # +++ 添加日志: 打印解析后的 JSON 数据 +++
                logger.debug(f"[ChatSummary] Parsed summary_data: {json.dumps(summary_data, indent=2, ensure_ascii=False)}")
                # +++++++++++++++++++++++++++++++++++++++
            except json.JSONDecodeError as e:
                error_msg = f"图片总结失败：无法解析模型返回的 JSON 数据。\n错误: {e}\n原始返回: {llm_response_text[:500]}..."
                logger.error(error_msg)
                e_context["reply"] = Reply(ReplyType.TEXT, error_msg)
                e_context.action = EventAction.BREAK_PASS # 修改为 BREAK_PASS
                logger.info(f"[ChatSummary] JSON parsing failed, setting action to BREAK_PASS") # 更新日志
                return
            except ValueError as e:
                 error_msg = f"图片总结失败：{e}\n原始返回: {llm_response_text[:500]}..."
                 logger.error(error_msg)
                 e_context["reply"] = Reply(ReplyType.TEXT, error_msg)
                 e_context.action = EventAction.BREAK_PASS # 修改为 BREAK_PASS
                 logger.info(f"[ChatSummary] JSON validation failed, setting action to BREAK_PASS") # 更新日志
                 return
            except Exception as e:
                 error_msg = f"图片总结失败：处理模型响应时出错。\n错误: {e}"
                 logger.error(error_msg, exc_info=True)
                 e_context["reply"] = Reply(ReplyType.TEXT, error_msg)
                 e_context.action = EventAction.BREAK_PASS # 修改为 BREAK_PASS
                 logger.info(f"[ChatSummary] Error processing LLM response, setting action to BREAK_PASS") # 更新日志
                 return

            # +++ 新增: 更新群聊名称 +++
            is_group_chat = e_context['context'].get("isgroup", False)
            if is_group_chat and session_id:
                logger.info(f"[ChatSummary] It's a group chat ({session_id}). Fetching group name via _get_group_nickname.")
                # _get_group_nickname now uses API -> config -> default "本群"
                final_group_name = self._get_group_nickname(session_id)

                # Ensure 'metadata' dictionary exists (using the key expected by image generation)
                # Note: The image generation part uses 'metadata' (English key) internally
                if 'metadata' not in summary_data: # Check/Create the key used by image gen
                    summary_data['metadata'] = {}
                    logger.warning("[ChatSummary] 'metadata' key was missing in summary_data, initialized.")

                # Get original LLM name just for logging comparison if needed
                original_llm_name = summary_data.get('metadata', {}).get('group_name', 'N/A')
                summary_data['metadata']['group_name'] = final_group_name
                logger.info(f"[ChatSummary] Set group name for image template to '{final_group_name}' (Source: API/Config/Default. Overwrites LLM value if any: '{original_llm_name}').")
            else:
                # 私聊时，明确设置为空
                logger.info("[ChatSummary] It's a private chat. Setting group name for image template to empty string.")
                summary_data['metadata']['group_name'] = ""
            # +++++++++++++++++++++++++++

            # 5. 调用渲染模块生成图片 (Uses the modified summary_data)
            logger.info("[ChatSummary] Generating summary image...")
            output_dir = str(Path(__file__).parent / "image_summary" / "output") # 输出目录
            image_path = None
            try:
                 # 检测是否为群聊场景 (前面已经获取)
                 # is_group_chat = e_context['context'].get("isgroup", False)
                 logger.info(f"[ChatSummary] 检测到{'群聊' if is_group_chat else '私聊'}环境，准备渲染")

                 # 使用 self.image_summarize_module 调用，传递群聊标志
                 image_path = self.image_summarize_module.generate_summary_image_from_data(
                     summary_data, 
                     output_dir, 
                     is_group_chat=is_group_chat
                 )
            except ImportError as dep_error: # 捕获渲染模块抛出的依赖错误
                 logger.error(f"图片生成失败，依赖项错误: {dep_error}")
                 reply_content = f"图片总结失败：缺少依赖库。{dep_error}"
                 e_context["reply"] = Reply(ReplyType.TEXT, reply_content)
                 e_context.action = EventAction.BREAK_PASS # 修改为 BREAK_PASS
                 logger.info(f"[ChatSummary] Dependency error during render, setting action to BREAK_PASS") # 更新日志
                 return
            except Exception as render_error: # 捕获渲染过程中的其他错误
                 logger.error(f"图片生成失败: {render_error}", exc_info=True)
                 reply_content = f"图片总结失败：渲染过程中出错 ({render_error})。"
                 e_context["reply"] = Reply(ReplyType.TEXT, reply_content)
                 e_context.action = EventAction.BREAK_PASS # 修改为 BREAK_PASS
                 logger.info(f"[ChatSummary] Render error, setting action to BREAK_PASS") # 更新日志
                 return

            # 6. 返回图片 Reply
            if image_path and os.path.exists(image_path):
                logger.info(f"[ChatSummary] Summary image generated: {image_path}")
                try:
                    # 读取图片内容为 BytesIO 对象
                    with open(image_path, 'rb') as f:
                        image_bytes = f.read()
                    image_io = io.BytesIO(image_bytes)
                    # 使用 BytesIO 对象设置 Reply
                    e_context["reply"] = Reply(ReplyType.IMAGE, image_io)
                    e_context.action = EventAction.BREAK_PASS # 修改为 BREAK_PASS
                    logger.info(f"[ChatSummary] Set IMAGE reply with BytesIO content and action to BREAK_PASS") # 更新日志
                except Exception as read_error:
                    logger.error(f"[ChatSummary] Failed to read image file or create BytesIO: {read_error}", exc_info=True)
                    reply_content = f"图片总结失败：读取生成的图片文件时出错。"
                    e_context["reply"] = Reply(ReplyType.TEXT, reply_content)
                    e_context.action = EventAction.BREAK_PASS # 即使读取失败也要 BREAK_PASS
                    logger.info(f"[ChatSummary] Error reading image file, setting action to BREAK_PASS") # 更新日志
            else:
                logger.error("[ChatSummary] Image generation failed, image_path is None or file does not exist.")
                
                # 检查是否有文本摘要备选方案
                text_summary = None
                try:
                    if self.image_summarize_module and hasattr(self.image_summarize_module, 'get_last_text_summary'):
                        text_summary = self.image_summarize_module.get_last_text_summary()
                except Exception as text_sum_error:
                    logger.error(f"[ChatSummary] Error retrieving text summary fallback: {text_sum_error}")
                
                # --- 修改失败提示 --- #
                if text_summary:
                    logger.info("[ChatSummary] Using text summary fallback after image generation failure.")
                    reply_content = f"图片生成失败（可能是由于内容过多或渲染引擎不稳定），已为您生成文本摘要：\n\n{text_summary}"
                else:
                    # 没有文本摘要，返回更具体的错误信息
                    logger.warning("[ChatSummary] Image generation failed and no text summary fallback available.")
                    error_hint = "可能是由于内容过多、渲染引擎不稳定或缺少依赖库。"
                    try:
                        # 尝试再次检查依赖，提供更准确的提示
                        if self.image_summarize_module:
                             self.image_summarize_module.check_dependencies()
                        else:
                             error_hint += " 渲染模块未加载。"
                    except ImportError as dep_error:
                        error_hint = f"缺少依赖库 ({dep_error})。"
                    
                    reply_content = f"图片总结失败（{error_hint}）请检查日志或尝试缩短总结范围。"
                # --- 结束修改 --- #
                
                e_context["reply"] = Reply(ReplyType.TEXT, reply_content)
                e_context.action = EventAction.BREAK_PASS # 修改为 BREAK_PASS

            # 添加最终 action 确认日志 (现在是 BREAK_PASS)
            logger.info(f"[ChatSummary] Setting action to {e_context.action} before returning from image handler.")
            return # 确保函数在此处返回

        except ValueError as ve: # 捕获会话 ID 错误等
             logger.error(f"[ChatSummary] 处理图片总结时出错: {ve}")
             e_context["reply"] = Reply(ReplyType.TEXT, f"图片总结失败: {ve}")
             e_context.action = EventAction.BREAK_PASS # 修改为 BREAK_PASS
             logger.info(f"[ChatSummary] ValueError caught, setting action to BREAK_PASS") # 更新日志
        except Exception as e:
            logger.error(f"[ChatSummary] 处理图片总结时发生意外错误: {e}", exc_info=True)
            e_context["reply"] = Reply(ReplyType.TEXT, f"图片总结失败：发生内部错误 ({e})。")
            e_context.action = EventAction.BREAK_PASS # 修改为 BREAK_PASS
            logger.info(f"[ChatSummary] Unexpected exception caught, setting action to BREAK_PASS") # 更新日志

    # +++ 重构: 获取群昵称，优先使用 GeweChat API +++
    def _get_group_nickname(self, group_wxid: str) -> str:
        """根据群 wxid 获取群昵称，优先尝试 GeweChat API。
        如果 API 成功获取名称，则使用该名称。
        其他所有情况 (API 禁用/失败/成功无名) 均返回 "本群"。
        不再检查 config.json 中的 group_name_mapping。

        Args:
            group_wxid: 群聊的 wxid (chatroomId)。

        Returns:
            获取到的群聊名称或默认值 "本群"。
        """
        # 1. 尝试 GeweChat API (如果启用且配置完整)
        if self.gewechat_enabled:
            # 1.1 检查缓存
            if group_wxid in self.group_name_cache:
                logger.debug(f"[ChatSummary] Found group nickname for {group_wxid} in cache.")
                return self.group_name_cache[group_wxid]

            # 1.2 调用 API
            api_url = self.gewechat_base_url.rstrip('/') + "/group/getChatroomInfo"
            headers = {
                'X-GEWE-TOKEN': self.gewechat_token,
                'Content-Type': 'application/json'
            }
            payload = json.dumps({
                "appId": self.gewechat_appid,
                "chatroomId": group_wxid
            })

            try:
                logger.debug(f"[ChatSummary] Calling GeweChat API for group {group_wxid} at {api_url}")
                response = requests.post(api_url, headers=headers, data=payload, timeout=10)

                if response.status_code == 200:
                    try:
                        result = response.json()
                        if result.get("ret") == 200:
                            nickName = result.get("data", {}).get("nickName")
                            if nickName:
                                logger.info(f"[ChatSummary] Successfully fetched group nickname '{nickName}' for {group_wxid} via GeweChat API.")
                                self.group_name_cache[group_wxid] = nickName # Update cache
                                return nickName
                            else:
                                logger.warning(f"[ChatSummary] GeweChat API returned success for {group_wxid} but nickname was missing. Falling back to default.")
                                return "本群" # API Success but no name -> Default
                        else:
                            error_msg = result.get("msg", "Unknown API logic error")
                            logger.warning(f"[ChatSummary] GeweChat API returned logic error for {group_wxid}: ret={result.get('ret')}, msg='{error_msg}'. Falling back to default.")
                            return "本群" # API logic error -> Default
                    except json.JSONDecodeError as json_err:
                        logger.error(f"[ChatSummary] Failed to parse GeweChat API JSON response for {group_wxid}: {json_err}. Response text: {response.text[:200]}... Falling back to default.")
                        return "本群" # JSON error -> Default
                    except Exception as parse_err:
                         logger.error(f"[ChatSummary] Error processing GeweChat API response for {group_wxid}: {parse_err}. Falling back to default.", exc_info=True)
                         return "本群" # Other parsing error -> Default
                else:
                    logger.error(f"[ChatSummary] GeweChat API request for {group_wxid} failed with status {response.status_code}. Response: {response.text[:200]}... Falling back to default.")
                    return "本群" # HTTP error -> Default

            except requests.exceptions.Timeout:
                logger.error(f"[ChatSummary] GeweChat API request for {group_wxid} timed out. Falling back to default.")
                return "本群" # Timeout -> Default
            except requests.exceptions.RequestException as req_err:
                logger.error(f"[ChatSummary] GeweChat API request for {group_wxid} failed: {req_err}. Falling back to default.")
                return "本群" # Network/Request error -> Default
            except Exception as e:
                 logger.error(f"[ChatSummary] Unexpected error during GeweChat API call for {group_wxid}: {e}. Falling back to default.", exc_info=True)
                 return "本群" # Unexpected error during API call -> Default
        else: # API disabled or misconfigured
            logger.debug(f"[ChatSummary] GeweChat API disabled or misconfigured for {group_wxid}. Falling back to default.")
            return "本群"

    # +++ 新增：清理函数 +++
    def _cleanup_output_files(self, directory: Path, max_age_hours: int):
        """清理指定目录下的旧 PNG 和 HTML 文件"""
        if not directory.is_dir():
            logger.warning(f"[ChatSummary Cleanup] Directory not found or not a directory: {directory}")
            return

        logger.info(f"[ChatSummary Cleanup] Starting cleanup for directory: {directory} (older than {max_age_hours} hours)")
        now = time.time()
        threshold_seconds = max_age_hours * 3600
        deleted_count = 0
        error_count = 0

        try:
            for item in directory.iterdir():
                try:
                    if item.is_file() and item.suffix.lower() in ['.png', '.html']:
                        file_mod_time = item.stat().st_mtime
                        file_age_seconds = now - file_mod_time
                        if file_age_seconds > threshold_seconds:
                            logger.info(f"[ChatSummary Cleanup] Deleting old file ({int(file_age_seconds / 3600)} hours old): {item.name}")
                            item.unlink() # 删除文件
                            deleted_count += 1
                except FileNotFoundError:
                     # 可能在迭代过程中文件被其他进程删除
                     logger.warning(f"[ChatSummary Cleanup] File not found during cleanup check (possibly already deleted): {item.name}")
                     continue
                except PermissionError:
                    logger.error(f"[ChatSummary Cleanup] Permission denied to delete file: {item.name}")
                    error_count += 1
                except Exception as e:
                    logger.error(f"[ChatSummary Cleanup] Error processing file {item.name}: {e}", exc_info=True)
                    error_count += 1
            logger.info(f"[ChatSummary Cleanup] Cleanup finished for {directory}. Deleted: {deleted_count} files. Errors: {error_count}.")
        except Exception as e:
            logger.error(f"[ChatSummary Cleanup] Error iterating directory {directory}: {e}", exc_info=True)

    # +++ 新增：调度器运行函数 +++
    def _run_scheduler(self):
        """运行定时任务调度器"""
        logger.info("[ChatSummary Scheduler] Scheduler thread started.")
        # 设置清理任务，目标目录和时间阈值从类属性获取
        schedule.every().day.at("03:00").do(self._cleanup_output_files, directory=self.cleanup_target_dir, max_age_hours=48)
        logger.info(f"[ChatSummary Scheduler] Scheduled cleanup task for {self.cleanup_target_dir} daily at 03:00 (older than 48h).")

        while True:
            try:
                schedule.run_pending()
                time.sleep(60) # 每分钟检查一次是否有任务需要运行
            except Exception as e:
                logger.error(f"[ChatSummary Scheduler] Error in scheduler loop: {e}", exc_info=True)
                # 即使出错也继续循环，但增加等待时间避免频繁报错
                time.sleep(300)