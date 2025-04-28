import os
import time
import logging
import base64
import json
import subprocess
import shutil
import tempfile
import platform
from pathlib import Path

# 尝试导入 playwright，如果失败则提供提示
try:
    from playwright.sync_api import sync_playwright, Error as PlaywrightError
except ImportError:
    logging.error("Playwright 库未安装。请运行 'pip install playwright && playwright install' 进行安装。")
    # 抛出异常或设置一个标志，以便在调用渲染函数时检查
    sync_playwright = None # 设置为 None 表示不可用
    PlaywrightError = None # 避免 NameError

try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
except ImportError:
    logging.error("Jinja2 库未安装。请运行 'pip install Jinja2' 进行安装。")
    Environment = None
    FileSystemLoader = None
    select_autoescape = None

# 获取插件目录
PLUGIN_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PLUGIN_DIR # 模板文件在当前目录
TEMPLATE_NAME = "image_summarize_template.html"
DEFAULT_OUTPUT_DIR = PLUGIN_DIR / "output" # 可以考虑将输出目录放到更合适的位置，例如项目根目录的 data/tmp

logger = logging.getLogger(__name__)

def check_dependencies():
    """检查 Playwright 和 Jinja2 是否已成功导入"""
    if sync_playwright is None:
        raise ImportError("Playwright 库未安装或导入失败。请运行 'pip install playwright && playwright install'。")
    if Environment is None:
        raise ImportError("Jinja2 库未安装或导入失败。请运行 'pip install Jinja2'。")

def check_wkhtmltopdf():
    """检查 wkhtmltopdf 是否已安装（作为备选渲染引擎）"""
    try:
        # 检查wkhtmltoimage命令是否可用
        result = subprocess.run(
            ["wkhtmltoimage", "--version"], 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE,
            text=True
        )
        if result.returncode == 0:
            logger.info(f"检测到wkhtmltopdf版本: {result.stdout.strip()}")
            return True
        return False
    except FileNotFoundError:
        logger.warning("wkhtmltopdf未安装或不可用。如需使用备选渲染引擎，请安装wkhtmltopdf。")
        return False
    except Exception as e:
        logger.warning(f"检查wkhtmltopdf时出错: {e}")
        return False

def generate_summary_html(summary_data: dict) -> str:
    """
    使用 Jinja2 将总结数据填充到 HTML 模板中。

    Args:
        summary_data: 从 LLM 获取并解析后的 JSON 数据字典。

    Returns:
        填充后的 HTML 字符串。
    """
    check_dependencies()

    try:
        env = Environment(
            loader=FileSystemLoader(TEMPLATE_DIR),
            autoescape=select_autoescape(['html', 'xml'])
        )
        template = env.get_template(TEMPLATE_NAME)

        # --- START KEY MAPPING ---
        mapped_context = {}

        # 1. Map Metadata (修正: 使用英文键名访问内部)
        metadata_cn = summary_data.get('metadata', {}) # 获取 metadata 字典，保持顶层键为'metadata'（假设ChatSummary.py是这样传的）
        if not isinstance(metadata_cn, dict):
             metadata_cn = {}

        mapped_context['metadata'] = {
            'group_name': metadata_cn.get('group_name') or '群聊',
            'date': metadata_cn.get('date') or time.strftime("%Y-%m-%d"),
            'total_messages': metadata_cn.get('total_messages') or 'N/A',
            'active_users': metadata_cn.get('active_users') or 'N/A',
            'time_range': metadata_cn.get('time_range') or 'N/A'
        }

        # 2. Map Hot Topics (使用英文键名: hot_topics, name, category, summary, keywords, mention_count)
        hot_topics_cn = summary_data.get('hot_topics', []) # 使用 JSON 键名: hot_topics
        mapped_context['hot_topics'] = []
        for topic_cn in hot_topics_cn:
            if isinstance(topic_cn, dict):
                mapped_context['hot_topics'].append({
                    'title': topic_cn.get('name'), # 使用 JSON 键名: name
                    'category': topic_cn.get('category'),
                    'summary': topic_cn.get('summary'),
                    'keywords': topic_cn.get('keywords', []),
                    'mention_count': topic_cn.get('mention_count')
                })

        # 3. Map Important Messages (基本正确，保留)
        important_messages_cn = summary_data.get('important_messages', []) # 使用 JSON 键名
        mapped_context['important_messages'] = []
        for msg_cn in important_messages_cn:
            if isinstance(msg_cn, dict):
                mapped_context['important_messages'].append({
                    'time': msg_cn.get('time'),
                    'sender': msg_cn.get('sender'),
                    'type': msg_cn.get('type'),
                    'priority': msg_cn.get('priority'),
                    'content': msg_cn.get('summary'), # JSON key is summary
                    'full_content': msg_cn.get('full_content')
                })

        # 4. Map Q&A Pairs (修正结构和键名)
        qa_pairs_cn = summary_data.get('qa_pairs', []) # 使用 JSON 键名
        mapped_context['qa_pairs'] = []
        for qa_cn in qa_pairs_cn:
            if isinstance(qa_cn, dict):
                # Map Question
                question_en = {
                    'asker': qa_cn.get('asker'),
                    'time': qa_cn.get('ask_time'), # JSON key is ask_time
                    'content': qa_cn.get('question'), # JSON key is question
                    'tags': qa_cn.get('tags', [])
                }

                # Map Answers (including best answer)
                answers_en = []
                best_answer_data = qa_cn.get('best_answer')
                if isinstance(best_answer_data, dict): # Check if best_answer is a dict
                    answers_en.append({
                        'responder': best_answer_data.get('answerer'), # JSON key is answerer
                        'time': best_answer_data.get('answer_time'), # JSON key is answer_time
                        'content': best_answer_data.get('content'),
                        'is_accepted': True
                    })
                
                # JSON does not seem to have supplementary answers in the provided log
                # supplementary_answers = qa_cn.get('supplementary_answers', [])
                # for ans_cn in supplementary_answers:
                #     if isinstance(ans_cn, dict):
                #         answers_en.append({...})

                mapped_context['qa_pairs'].append({
                    'question': question_en,
                    'answers': answers_en
                })

        # 5. Map Tutorials (修正键名: tutorials_resources)
        tutorials_cn = summary_data.get('tutorials_resources', []) # 使用 JSON 键名
        mapped_context['tutorials'] = []
        for tut_cn in tutorials_cn:
            if isinstance(tut_cn, dict):
                mapped_context['tutorials'].append({
                    'type': tut_cn.get('type'),
                    'title': tut_cn.get('title'),
                    'shared_by': tut_cn.get('sharer'), # JSON key is sharer
                    'time': tut_cn.get('time'),
                    'summary': tut_cn.get('summary'),
                    'key_points': tut_cn.get('key_points', []),
                    'link': tut_cn.get('link'),
                    'category': tut_cn.get('category')
                })

        # 6. Map Dialogues (修正键名: fun_content, dialogue, speaker, content)
        dialogues_cn = summary_data.get('fun_content', []) # 使用 JSON 键名
        mapped_context['dialogues'] = []
        for dlg_cn in dialogues_cn:
            if isinstance(dlg_cn, dict):
                # Map dialogue messages
                messages_en = []
                dialogue_list = dlg_cn.get('dialogue', []) # JSON key is dialogue
                for msg_item in dialogue_list:
                    if isinstance(msg_item, dict):
                        # Format as string like "[speaker] (time): content"
                        speaker = msg_item.get('speaker', '?')
                        time_str = msg_item.get('time', '?')
                        content = msg_item.get('content', '')
                        messages_en.append(f"[{speaker}] ({time_str}): {content}")
                
                mapped_context['dialogues'].append({
                    'type': dlg_cn.get('type'),
                    'messages': messages_en, # Pass the formatted list of strings
                    'time': dlg_cn.get('time'), # This top-level time might be less relevant now
                    'highlight': dlg_cn.get('highlight'),
                    'related_topic': dlg_cn.get('related_topic')
                })

        # 7. Map Analytics
        analytics_cn = summary_data.get('data_analysis', {}) # Use JSON key
        mapped_context['analytics'] = {}
        
        # 7.1 Map Heatmap (修正键名: topic_heat, 并适配字典结构)
        heatmap_cn = analytics_cn.get('topic_heat', []) # Use JSON key
        mapped_context['analytics']['heatmap'] = []
        for item_dict in heatmap_cn:
            if isinstance(item_dict, dict):
                mapped_context['analytics']['heatmap'].append({
                    'topic': item_dict.get('topic_name'), # JSON key
                    'percentage': str(item_dict.get('percentage', '0')).replace('%',''), # Ensure % is removed
                    'count': item_dict.get('message_count'), # JSON key
                    'color': item_dict.get('color')
                })
            # Remove the old string parsing logic
            # else: # Fallback if format is unexpected
            #      mapped_context['analytics']['heatmap'].append({'raw': item_str})

        # 7.2 Map Participants (Top Talkers) (修正键名: top_chatters, user_profile, frequent_words)
        participants_cn = analytics_cn.get('top_chatters', []) # Use JSON key
        mapped_context['analytics']['participants'] = []
        for p_cn in participants_cn:
            if isinstance(p_cn, dict):
                mapped_context['analytics']['participants'].append({
                    'rank': p_cn.get('rank'),
                    'name': p_cn.get('nickname'),
                    'message_count': p_cn.get('message_count'),
                    'profile': p_cn.get('user_profile'), # JSON key
                    'keywords': p_cn.get('frequent_words', []) # JSON key
                })

        # 7.3 Map Night Owl (基本正确，保留)
        night_owl_cn = analytics_cn.get('night_owl', {})
        if night_owl_cn: # Check if dict is not empty
             mapped_context['analytics']['night_owl'] = {
                 'name': night_owl_cn.get('nickname'),
                 'time': night_owl_cn.get('latest_active_time'), # JSON key
                 'message_count': night_owl_cn.get('late_night_messages'), # JSON key
                 'message': night_owl_cn.get('representative_message'), # JSON key
                 'title': night_owl_cn.get('title')
             }
        
        # 8. Map Word Cloud (基本正确，保留)
        word_cloud_cn = summary_data.get('word_cloud', []) # Use JSON key
        mapped_context['word_cloud'] = []
        for word_cn in word_cloud_cn:
            if isinstance(word_cn, dict):
                mapped_context['word_cloud'].append({
                    'text': word_cn.get('word'), # JSON key
                    'size': word_cn.get('size'), # JSON key
                    'color': word_cn.get('color')
                })

        # Add generation time
        mapped_context['generation_time'] = time.strftime("%Y-%m-%d %H:%M:%S")
        # --- END KEY MAPPING ---

        # Log the mapped context
        logger.debug(f"[Generate HTML Context - Mapped] {json.dumps(mapped_context, indent=2, ensure_ascii=False)}")

        # Render using the mapped context
        html_content = template.render(mapped_context)
        return html_content

    except FileNotFoundError:
        logger.error(f"HTML 模板文件未找到: {TEMPLATE_NAME} in {TEMPLATE_DIR}")
        raise
    except ImportError as e: # 捕获依赖检查的错误
        logger.error(f"依赖项检查失败: {e}")
        raise
    except Exception as e:
        logger.error(f"使用 Jinja2 生成 HTML 时出错: {e}", exc_info=True)
        raise

def render_html_to_image(html_content: str, output_path: str) -> bool:
    """
    使用 Playwright 将 HTML 内容渲染为图片 (增强版)。
    改进：简化启动参数、增强错误监听、重试机制、稳定性改进。
    """
    check_dependencies() # 确保 Playwright 可用

    # 确保输出目录存在
    output_dir = Path(output_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"开始渲染 HTML 到图片: {output_path}")
    browser = None # 初始化 browser 变量
    context = None # 初始化 context 变量
    page = None # 初始化 page 变量
    
    # 最大重试次数
    max_retries = 2
    current_retry = 0
    
    while current_retry <= max_retries:
        try:
            with sync_playwright() as p:
                # 简化的浏览器启动参数，只保留必要的安全和稳定性参数
                browser_args = [
                    '--disable-gpu',
                    '--disable-dev-shm-usage',
                    '--disable-setuid-sandbox',
                    '--no-sandbox',
                    '--disable-extensions',
                    '--disable-background-networking',
                    '--disable-background-timer-throttling',
                ]
                
                # 对启动进行错误处理
                try:
                    browser = p.chromium.launch(
                        headless=True,
                        args=browser_args,
                        timeout=60000 # 减少启动超时
                    )
                except PlaywrightError as e:
                    logger.error(f"启动 Playwright 浏览器失败: {e}. 请确保已运行 'playwright install'.")
                    if current_retry < max_retries:
                        current_retry += 1
                        logger.info(f"正在重试 ({current_retry}/{max_retries})...")
                        continue
                    return False
                except Exception as e:
                    logger.error(f"启动 Playwright 浏览器时发生未知错误: {e}", exc_info=True)
                    if current_retry < max_retries:
                        current_retry += 1
                        logger.info(f"正在重试 ({current_retry}/{max_retries})...")
                        continue
                    return False

                # 配置上下文和页面，降低设备缩放因子以减少渲染压力
                context = browser.new_context(
                    viewport={'width': 1200, 'height': 1200},
                    bypass_csp=True,
                    device_scale_factor=1.0 # 降低到1.0以减少渲染压力
                )
                context.set_default_timeout(30000) # 减少到30秒
                page = context.new_page()
                page.set_default_timeout(30000) # 减少到30秒
                page.set_default_navigation_timeout(30000)

                # 添加错误监听
                page.on("pageerror", lambda err: logger.error(f"[Playwright Page Error] {err}"))
                page.on("crash", lambda: logger.error("[Playwright Page Crashed]"))

                # 使用更稳定的方式设置页面内容
                try:
                    # 修改HTML内容，内联JavaScript以确保它在截图前执行完成
                    inline_js = """
                    <script>
                    // 立即执行的脚本，不等待DOMContentLoaded
                    (function() {
                        // 设置一个标志表示页面处理完成
                        window.renderingCompleted = false;
                        
                        // 热度条和词云样式设置函数
                        function applyStyles() {
                            // 设置热度条样式
                            document.querySelectorAll('.heat-fill').forEach(function(el) {
                                var percentage = el.getAttribute('data-percentage');
                                var color = el.getAttribute('data-color');
                                el.style.width = percentage + '%';
                                el.style.backgroundColor = color;
                            });

                            // 设置词云样式
                            document.querySelectorAll('.cloud-word').forEach(function(el) {
                                var size = el.getAttribute('data-size');
                                var color = el.getAttribute('data-color');
                                el.style.fontSize = size + 'px';
                                el.style.color = color;
                            });
                            
                            // 标记渲染完成
                            window.renderingCompleted = true;
                            console.log('All styles applied successfully');
                        }
                        
                        // 立即尝试应用样式
                        applyStyles();
                        
                        // 如果DOM尚未准备好，在加载后再次尝试
                        if (document.readyState === 'loading') {
                            document.addEventListener('DOMContentLoaded', applyStyles);
                        }
                    })();
                    </script>
                    """
                    
                    # 在HTML结束前插入内联脚本
                    modified_html = html_content.replace('</body>', f'{inline_js}</body>')
                    
                    # 设置页面内容并等待
                    page.set_content(modified_html, wait_until="networkidle", timeout=30000)
                    logger.info("[Playwright] 页面内容加载完成")
                    
                    # 等待渲染处理完成
                    page.wait_for_timeout(500)  # 先给一个短暂的初始等待
                    
                    # 检查渲染是否完成
                    rendering_complete = page.evaluate("""
                        () => {
                            return window.renderingCompleted === true;
                        }
                    """)
                    
                    if not rendering_complete:
                        logger.warning("[Playwright] 等待样式应用完成...")
                        # 如果未完成，额外等待
                        page.wait_for_timeout(1000)
                    
                    logger.info("[Playwright] 页面渲染处理完成")
                    
                    # 动态计算内容高度
                    content_height = page.evaluate("""
                        () => {
                            const container = document.body;
                            if (!container) return 1200;
                            return Math.max(container.scrollHeight, container.clientHeight, 800);
                        }
                    """)
                    logger.info(f"[Playwright] 检测到内容高度: {content_height}px")

                    # 调整视口高度以适应内容
                    target_height = int(content_height) + 50
                    target_width = 1200
                    page.set_viewport_size({"width": target_width, "height": target_height})
                    page.wait_for_timeout(300) # 等待调整生效
                    
                    # 截图策略与错误处理
                    logger.info(f"[Playwright] 尝试截图，目标区域: {target_width}x{target_height}")
                    screenshot_success = False
                    
                    # 使用更简单的截图方法，减少复杂性
                    try:
                        # 直接完整页面截图，更可靠
                        page.screenshot(path=output_path, full_page=True, type='png', timeout=30000)
                        logger.info(f"[Playwright] 完整页面截图成功: {output_path}")
                        screenshot_success = True
                    except Exception as screenshot_error:
                        logger.error(f"[Playwright] 截图失败: {screenshot_error}", exc_info=True)
                        # 如果还有重试机会，继续
                        if current_retry < max_retries:
                            current_retry += 1
                            logger.info(f"正在重试 ({current_retry}/{max_retries})...")
                            # 确保资源被正确关闭
                            if page:
                                try:
                                    page.close()
                                except Exception as close_error:
                                    logger.warning(f"[Playwright] 关闭页面时出错: {close_error}")
                            if context:
                                try:
                                    context.close()
                                except Exception as close_error:
                                    logger.warning(f"[Playwright] 关闭上下文时出错: {close_error}")
                            if browser:
                                try:
                                    browser.close()
                                except Exception as close_error:
                                    logger.warning(f"[Playwright] 关闭浏览器时出错: {close_error}")
                            continue
                        screenshot_success = False

                    return screenshot_success

                except PlaywrightError as e:
                    logger.error(f"Playwright 页面操作时出错: {e}", exc_info=True)
                    if current_retry < max_retries:
                        current_retry += 1
                        logger.info(f"正在重试 ({current_retry}/{max_retries})...")
                        continue
                    return False
                except Exception as e:
                    logger.error(f"Playwright 页面操作时发生意外错误: {e}", exc_info=True)
                    if current_retry < max_retries:
                        current_retry += 1
                        logger.info(f"正在重试 ({current_retry}/{max_retries})...")
                        continue
                    return False

        except PlaywrightError as e:
            logger.error(f"Playwright 渲染过程中出错: {e}", exc_info=True)
            if current_retry < max_retries:
                current_retry += 1
                logger.info(f"正在重试 ({current_retry}/{max_retries})...")
                continue
            return False
        except Exception as e:
            logger.error(f"渲染 HTML 到图片时发生意外错误: {e}", exc_info=True)
            if current_retry < max_retries:
                current_retry += 1
                logger.info(f"正在重试 ({current_retry}/{max_retries})...")
                continue
            return False
        finally:
            # The `with sync_playwright()` context manager handles closing resources automatically.
            logger.debug("[Playwright] Resources will be closed automatically by the context manager.")
            # Explicit closing calls removed as they are handled by the context manager and cause errors.
    
    # 如果重试用尽但仍然失败
    return False

def render_with_wkhtmltopdf(html_content, output_path):
    """
    使用wkhtmltopdf作为备选渲染引擎将HTML渲染为图片
    
    Args:
        html_content: HTML内容
        output_path: 输出图片路径
        
    Returns:
        成功返回True，失败返回False
    """
    if not check_wkhtmltopdf():
        logger.error("备选渲染引擎wkhtmltopdf不可用")
        return False
    
    try:
        # 创建临时HTML文件
        with tempfile.NamedTemporaryFile(suffix='.html', delete=False, mode='w', encoding='utf-8') as temp_html:
            temp_html_path = temp_html.name
            temp_html.write(html_content)
        
        # 确保输出目录存在
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        
        # 使用wkhtmltoimage渲染
        logger.info(f"[wkhtmltopdf] 使用备选引擎渲染HTML到图片: {output_path}")
        
        # 设置适当的参数
        command = [
            "wkhtmltoimage",
            "--enable-local-file-access",
            "--quality", "90",
            "--width", "1200",
            "--disable-smart-width",
            "--no-stop-slow-scripts",
            "--javascript-delay", "1000",  # 等待1秒让JS执行
            temp_html_path,
            output_path
        ]
        
        # 执行命令
        process = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30  # 30秒超时
        )
        
        # 检查结果
        if process.returncode == 0 and os.path.exists(output_path):
            logger.info(f"[wkhtmltopdf] 渲染成功: {output_path}")
            success = True
        else:
            logger.error(f"[wkhtmltopdf] 渲染失败: {process.stderr}")
            success = False
            
        # 清理临时文件
        try:
            os.unlink(temp_html_path)
        except Exception as e:
            logger.warning(f"清理临时文件时出错: {e}")
            
        return success
    except Exception as e:
        logger.error(f"使用wkhtmltopdf渲染时出错: {e}", exc_info=True)
        return False

def generate_text_summary(summary_data):
    """
    生成纯文本摘要作为备选输出
    
    Args:
        summary_data: 从LLM获取的解析后JSON数据
        
    Returns:
        纯文本摘要
    """
    try:
        lines = []
        
        # 添加标题和元数据 (修正: 使用英文键名)
        metadata = summary_data.get('metadata', {}) # 获取 metadata 字典
        if not isinstance(metadata, dict):
            metadata = {}

        group_name_txt = metadata.get('group_name', '群聊')
        date_txt = metadata.get('date', '未知日期')
        total_messages_txt = metadata.get('total_messages', 'N/A')
        active_users_txt = metadata.get('active_users', 'N/A')
        time_range_txt = metadata.get('time_range', 'N/A')

        lines.append(f"📱 {group_name_txt}日报 - {date_txt}")
        lines.append(f"📊 总消息: {total_messages_txt} | 活跃用户: {active_users_txt} | 时间范围: {time_range_txt}")
        lines.append("")
        
        # 今日热点 (修正: 使用英文键名 hot_topics, name, summary, keywords)
        hot_topics = summary_data.get('hot_topics', [])
        if hot_topics:
            lines.append("🔥 今日讨论热点:")
            for i, topic in enumerate(hot_topics, 1):
                if isinstance(topic, dict):
                    lines.append(f"{i}. {topic.get('name', '未知话题')}") # Use 'name'
                    if topic.get('summary'):
                        lines.append(f"   {topic.get('summary')}")
                    if topic.get('keywords'):
                        lines.append(f"   关键词: {', '.join(topic.get('keywords', []))}") # Use 'keywords'
            lines.append("")
        
        # 重要消息 (修正: 使用英文键名 important_messages, time, sender, summary)
        important_msgs = summary_data.get('important_messages', [])
        if important_msgs:
            lines.append("📢 重要消息汇总:")
            for i, msg in enumerate(important_msgs, 1):
                if isinstance(msg, dict):
                    sender = msg.get('sender', '未知用户')
                    time_val = msg.get('time', '未知时间') # Use 'time'
                    content = msg.get('summary', '无内容') # Use 'summary'
                    lines.append(f"{i}. [{time_val}] {sender}: {content}")
            lines.append("")
        
        # 数据分析部分 (修正: 使用英文键名 data_analysis, topic_heat, top_chatters, night_owl)
        analytics = summary_data.get('data_analysis', {}) # Use 'data_analysis'
        if analytics:
            lines.append("📊 数据分析:")
            
            # 话题热度 (修正: 使用英文键名 topic_heat, topic_name, message_count)
            heatmap = analytics.get('topic_heat', []) # Use 'topic_heat'
            if heatmap:
                lines.append("· 话题热度:")
                for item in heatmap:
                    # 假设item是字典，直接使用英文key
                    if isinstance(item, dict):
                        topic_name = item.get('topic_name', 'N/A') # Use 'topic_name'
                        percentage = item.get('percentage', 'N/A')
                        msg_count = item.get('message_count', 'N/A') # Use 'message_count'
                        lines.append(f"  - {topic_name} ({percentage}) - {msg_count}条")
                    elif isinstance(item, str): # 保留旧格式兼容性
                        parts = item.split('|')
                        if len(parts) >= 2:
                            topic_part = parts[0]
                            count_part = parts[1]
                            lines.append(f"  - {topic_part.strip()} ({count_part.strip()})")
            
            # 话唠榜 (修正: 使用英文键名 top_chatters, rank, nickname, message_count)
            talkers = analytics.get('top_chatters', []) # Use 'top_chatters'
            if talkers:
                lines.append("· 话唠榜TOP3:")
                for talker in talkers:
                    if isinstance(talker, dict):
                        rank = talker.get('rank', '?')
                        name = talker.get('nickname', '匿名')
                        count = talker.get('message_count', '0') # Use 'message_count'
                        lines.append(f"  {rank}. {name} - {count}条消息")
            
            # 熬夜冠军 (修正: 使用英文键名 night_owl, nickname, latest_active_time, late_night_messages)
            night_owl = analytics.get('night_owl', {}) # Use 'night_owl'
            if night_owl:
                name = night_owl.get('nickname', '匿名')
                time_val = night_owl.get('latest_active_time', '未知') # Use 'latest_active_time'
                count = night_owl.get('late_night_messages', '0') # Use 'late_night_messages'
                lines.append(f"· 熬夜冠军: {name} - 最晚活跃: {time_val}, 深夜消息: {count}条")
            
            lines.append("")
        
        # 词云 (修正: 使用英文键名 word_cloud, word)
        word_cloud = summary_data.get('word_cloud', []) # Use 'word_cloud'
        if word_cloud:
            words = [item.get('word', '') for item in word_cloud if isinstance(item, dict)] # Use 'word'
            if words:
                lines.append("🔤 热门词汇: " + ", ".join(words[:10]) + (len(words) > 10 and "..." or ""))
                lines.append("")
        
        # 添加生成时间
        lines.append(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"生成文本摘要时出错: {e}", exc_info=True)
        return f"生成摘要失败: {str(e)}\n原始数据: {json.dumps(summary_data, ensure_ascii=False)[:500]}..."

def sanitize_summary_data(summary_data: dict, max_items=10, max_text_length=500) -> dict:
    """
    清理和限制JSON数据大小，防止过大导致渲染问题
    
    Args:
        summary_data: 原始JSON数据
        max_items: 每个列表类型最大条目数
        max_text_length: 文本字段最大长度
        
    Returns:
        清理后的数据
    """
    if not isinstance(summary_data, dict):
        return {}
    
    result = {}
    try:
        # 处理元数据 - 保持顶层中文键 '元数据', 内部处理英文键
        if "metadata" in summary_data:
            meta_in = summary_data["metadata"]
            if isinstance(meta_in, dict):
                 # 这里假设 sanitize 只限制长度和数量，不改变键名
                 result["metadata"] = {k: str(v)[:max_text_length] if isinstance(v, (str, int, float)) else v for k, v in meta_in.items()}

        # 处理热点话题 - 保持顶层中文键 '今日讨论热点', 内部处理英文键
        if "hot_topics" in summary_data:
            topics_in = summary_data["hot_topics"]
            if isinstance(topics_in, list):
                result["hot_topics"] = []
                for i, topic in enumerate(topics_in):
                    if i >= max_items: break
                    if isinstance(topic, dict):
                        clean_topic = {}
                        for k, v in topic.items():
                            if k == "keywords" and isinstance(v, list):
                                clean_topic[k] = v[:5]
                            elif isinstance(v, str):
                                clean_topic[k] = v[:max_text_length]
                            else:
                                clean_topic[k] = v
                        result["hot_topics"].append(clean_topic)

        # 处理重要消息 - 保持顶层中文键 '重要消息', 内部处理英文键
        if "important_messages" in summary_data:
            messages_in = summary_data["important_messages"]
            if isinstance(messages_in, list):
                result["important_messages"] = []
                for i, msg in enumerate(messages_in):
                    if i >= max_items: break
                    if isinstance(msg, dict):
                        clean_msg = {}
                        for k, v in msg.items():
                            if isinstance(v, str):
                                clean_msg[k] = v[:max_text_length]
                            else:
                                clean_msg[k] = v
                        result["important_messages"].append(clean_msg)

        # 处理问答对 - 保持顶层中文键 '问答对', 内部处理英文键
        if "qa_pairs" in summary_data:
            qa_pairs_in = summary_data["qa_pairs"]
            if isinstance(qa_pairs_in, list):
                # 保持英文键结构，只限制数量
                result["qa_pairs"] = qa_pairs_in[:max_items//2]

        # 处理数据分析 - 保持顶层中文键 '数据分析', 内部处理英文键
        if "data_analysis" in summary_data:
            analytics_in = summary_data["data_analysis"]
            if isinstance(analytics_in, dict):
                result["data_analysis"] = {}
                if "topic_heat" in analytics_in and isinstance(analytics_in["topic_heat"], list):
                    result["data_analysis"]["topic_heat"] = analytics_in["topic_heat"][:max_items]
                if "top_chatters" in analytics_in and isinstance(analytics_in["top_chatters"], list):
                    result["data_analysis"]["top_chatters"] = analytics_in["top_chatters"][:3]
                if "night_owl" in analytics_in: # night_owl is dict
                    result["data_analysis"]["night_owl"] = analytics_in["night_owl"]


        # 处理词云 - 保持顶层中文键 '词云', 内部处理英文键
        if "word_cloud" in summary_data:
            word_cloud_in = summary_data["word_cloud"]
            if isinstance(word_cloud_in, list):
                result["word_cloud"] = word_cloud_in[:15]

        # 处理其他可能存在的顶层中文键（如果需要）
        # 例如：实用教程与资源, 趣味内容
        if "tutorials_resources" in summary_data:
            tut_res_in = summary_data["tutorials_resources"]
            if isinstance(tut_res_in, list):
                result["tutorials_resources"] = tut_res_in[:max_items]

        if "fun_content" in summary_data:
            fun_in = summary_data["fun_content"]
            if isinstance(fun_in, list):
                 result["fun_content"] = fun_in[:max_items]


        # 返回清理后的数据，注意这里返回的结构可能混合了中英文顶层键
        # 但 generate_summary_html/generate_lite_html 会根据英文键获取数据
        return result
    except Exception as e:
        logger.error(f"清理数据时出错: {e}", exc_info=True)
        return summary_data  # 出错则返回原始数据

def generate_lite_html(summary_data: dict) -> str:
    """
    生成极简版HTML，无JavaScript，最小化CSS，用于群聊场景
    
    Args:
        summary_data: 从LLM获取的解析后JSON数据
        
    Returns:
        简化的HTML内容
    """
    try:
        # 引入时间模块，使用不同名称避免命名冲突
        import time as time_module
        
        # 获取元数据 (修正: 使用英文键名访问内部)
        metadata = summary_data.get('metadata', {}) # 获取 metadata 字典
        if not isinstance(metadata, dict):
            metadata = {}

        group_name = metadata.get('group_name') or '群聊'
        date = metadata.get('date') or time_module.strftime("%Y-%m-%d")
        total_messages = metadata.get('total_messages') or 'N/A'
        active_users = metadata.get('active_users') or 'N/A'
        time_range = metadata.get('time_range') or 'N/A'
        
        # 构建HTML头部
        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{group_name}日报 - {date}</title>
    <style>
        /* 基础样式 */
        :root {{
            --bg-primary: #0f0e17;
            --bg-secondary: #1a1925;
            --bg-tertiary: #252336;
            --text-primary: #fffffe;
            --text-secondary: #a7a9be;
            --accent-primary: #ff8906;
            --accent-blue: #3da9fc;
            --accent-secondary: #f25f4c;
            --accent-tertiary: #e53170;
            --accent-purple: #7209b7;
            --accent-cyan: #00b4d8;
        }}
        
        @font-face {{
            font-family: 'CustomFont';
            src: local('SF Pro Display'), local('Segoe UI'), local('Roboto'), local('PingFang SC'), local('Microsoft YaHei'), local('微软雅黑'), local('黑体'), local('Helvetica Neue'), local('Arial'), sans-serif;
            font-display: swap;
        }}
        
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: 'CustomFont', 'SF Pro Display', 'Segoe UI', 'Roboto', 'PingFang SC', 'Microsoft YaHei', '微软雅黑', sans-serif;
            background-color: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.6;
            font-size: 16px;
            width: 1200px;
            margin: 0 auto;
            padding: 20px;
            text-rendering: optimizeLegibility;
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
        }}
        
        /* 头部样式 */
        header {{
            text-align: center;
            padding: 24px 0;
            background-color: var(--bg-secondary);
            margin-bottom: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
        }}
        
        h1 {{
            font-size: 28px;
            color: var(--accent-primary);
            margin-bottom: 6px;
            font-weight: 600;
        }}
        
        h2 {{
            font-size: 22px;
            color: var(--accent-blue);
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 1px solid var(--accent-blue);
            font-weight: 600;
        }}
        
        h3 {{
            font-size: 18px;
            color: var(--accent-primary);
            margin: 12px 0 10px 0;
            font-weight: 600;
        }}
        
        .date {{
            font-size: 16px;
            color: var(--text-secondary);
            margin-bottom: 10px;
        }}
        
        .meta-info {{
            display: flex;
            justify-content: center;
            gap: 15px;
            font-size: 14px;
        }}
        
        .meta-info span {{
            background-color: var(--bg-tertiary);
            padding: 4px 12px;
            border-radius: 15px;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.15);
        }}
        
        section {{
            background-color: var(--bg-secondary);
            margin-bottom: 20px;
            padding: 22px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.15);
        }}
        
        /* 卡片样式 */
        .card {{
            background-color: var(--bg-tertiary);
            padding: 18px;
            margin-bottom: 16px;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12);
            transition: box-shadow 0.2s ease;
        }}
        
        .meta {{
            color: var(--text-secondary);
            font-size: 13px;
            margin-bottom: 6px;
        }}
        
        .meta span {{
            margin-right: 10px;
        }}
        
        /* 标签和关键词样式 */
        .keyword {{
            display: inline-block;
            background-color: rgba(61, 169, 252, 0.15);
            padding: 2px 10px;
            border-radius: 12px;
            font-size: 13px;
            margin-right: 6px;
            margin-bottom: 6px;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
        }}
        
        .tag {{
            background-color: rgba(61, 169, 252, 0.15);
            padding: 2px 10px;
            border-radius: 12px;
            font-size: 13px;
            display: inline-block;
            margin-right: 6px;
            margin-bottom: 6px;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
        }}
        
        /* 统计部分样式 */
        .heat-item {{
            display: grid;
            grid-template-columns: auto 1fr auto;
            gap: 12px;
            align-items: center;
            background-color: var(--bg-tertiary);
            padding: 10px 14px;
            margin-bottom: 10px;
            border-radius: 8px;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
        }}
        
        .heat-topic {{
            font-weight: 600;
        }}
        
        .heat-bar {{
            height: 16px;
            background-color: rgba(255, 255, 255, 0.08);
            border-radius: 8px;
            overflow: hidden;
            position: relative;
            box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.1);
        }}
        
        .heat-fill {{
            height: 100%;
            border-radius: 8px;
            position: absolute;
            left: 0;
            top: 0;
        }}
        
        .heat-count {{
            color: var(--text-secondary);
            text-align: right;
            white-space: nowrap;
            font-weight: 500;
        }}
        
        /* 用户排行榜样式 */
        .participants-container {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 16px;
        }}
        
        .participant-item {{
            background-color: var(--bg-tertiary);
            padding: 16px;
            margin-bottom: 12px;
            border-radius: 8px;
            display: flex;
            gap: 16px;
            align-items: flex-start;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12);
        }}
        
        .participant-rank {{
            font-size: 24px;
            font-weight: 700;
            color: var(--accent-primary);
            line-height: 1.2;
            min-width: 24px;
            text-align: center;
        }}
        
        .participant-info {{
            flex-grow: 1;
        }}
        
        .participant-name {{
            font-weight: 600;
            font-size: 16px;
            margin-bottom: 3px;
        }}
        
        .participant-count {{
            color: var(--accent-cyan);
            margin-bottom: 8px;
            font-size: 13px;
            font-weight: 500;
        }}
        
        /* 熬夜冠军样式 */
        .night-owl-item {{
            background-color: var(--bg-tertiary);
            padding: 18px;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12);
        }}
        
        .owl-crown {{
            font-size: 22px;
            display: inline-block;
            margin-right: 10px;
            color: var(--accent-primary);
        }}
        
        /* 词云样式 */
        .word-cloud-container {{
            background-color: var(--bg-tertiary);
            padding: 24px;
            text-align: center;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12);
        }}
        
        .cloud-word {{
            display: inline-block;
            padding: 4px 10px;
            margin: 5px;
            border-radius: 12px;
            background-color: rgba(255, 255, 254, 0.08);
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
        }}
        
        /* 问答样式 */
        .qa-question {{
            border-left: 3px solid var(--accent-blue);
            padding-left: 12px;
            margin-bottom: 15px;
        }}
        
        .qa-best-answer {{
            background-color: rgba(255, 137, 6, 0.08);
            border-radius: 8px;
            padding: 14px;
            margin-top: 12px;
            margin-bottom: 10px;
            border-left: 3px solid var(--accent-primary);
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
        }}
        
        .qa-answer {{
            background-color: rgba(255, 255, 255, 0.05);
            padding: 14px;
            margin-bottom: 10px;
            border-radius: 8px;
            border-left: 2px solid var(--text-secondary);
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
        }}
        
        .badge {{
            background-color: var(--accent-primary);
            color: var(--text-primary);
            padding: 2px 10px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 500;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.15);
        }}
        
        /* 趣味内容样式 */
        .dialogue-badge {{
            display: inline-block;
            background-color: var(--accent-tertiary);
            color: var(--text-primary);
            padding: 3px 12px;
            border-radius: 15px;
            font-size: 14px;
            margin-bottom: 12px;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.15);
        }}
        
        .dialogue-content {{
            background-color: rgba(255, 255, 255, 0.05);
            padding: 12px;
            margin: 12px 0;
            font-size: 14px;
            border-radius: 8px;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
        }}
        
        .dialogue-highlight {{
            font-style: italic;
            color: var(--accent-primary);
            margin: 12px 0;
            font-weight: 600;
        }}
        
        /* 页脚样式 */
        footer {{
            text-align: center;
            padding: 18px 0;
            margin-top: 30px;
            background-color: var(--bg-secondary);
            color: var(--text-secondary);
            font-size: 13px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.15);
        }}
    </style>
</head>
<body>
    <header>
        <h1>{group_name}日报</h1>
        <p class="date">{date}</p>
        <div class="meta-info">
            <span>总消息数：{total_messages}</span> 
            <span>活跃用户：{active_users}</span> 
            <span>时间范围：{time_range}</span>
        </div>
    </header>
"""
        
        # 添加热点话题
        hot_topics = summary_data.get('hot_topics', [])
        if hot_topics:
            html += '<section>\n<h2>今日讨论热点</h2>\n'
            for topic in hot_topics:
                if isinstance(topic, dict):
                    title = topic.get('name', '未知话题')
                    category = topic.get('category', '')
                    summary = topic.get('summary', '无总结')
                    keywords = topic.get('keywords', [])
                    
                    html += f'<div class="card">\n'
                    html += f'<h3>{title}</h3>\n'
                    if category:
                        html += f'<div class="meta"><span>分类: {category}</span></div>\n'
                    html += f'<p style="margin-bottom:10px;">{summary}</p>\n'
                    if keywords:
                        html += '<div class="keywords" style="margin-top:12px;display:flex;flex-wrap:wrap;">'
                        for keyword in keywords:
                            html += f'<span class="keyword">{keyword}</span>'
                        html += '</div>\n'
                    html += '</div>\n'
            html += '</section>\n'
        
        # 添加重要消息
        important_messages = summary_data.get('important_messages', [])
        if important_messages:
            html += '<section>\n<h2>重要消息汇总</h2>\n'
            for msg in important_messages:
                if isinstance(msg, dict):
                    msg_time = msg.get('time', '未知时间')  # 重命名变量避免冲突
                    sender = msg.get('sender', '未知用户')
                    msg_type = msg.get('type', '')
                    priority = msg.get('priority', '')
                    content = msg.get('summary', '无内容')
                    
                    html += f'<div class="card">\n'
                    html += f'<div class="meta">'
                    html += f'<span>{msg_time}</span> <span>{sender}</span>'
                    if msg_type:
                        html += f' <span>类型: {msg_type}</span>'
                    if priority:
                        priority_class = f"priority-{priority.lower()}" if priority.lower() in ['high', 'medium', 'low'] else ""
                        priority_color = "#f25f4c" if priority.lower() == 'high' else "#ff8906" if priority.lower() == 'medium' else "#3da9fc"
                        html += f' <span class="badge" style="background-color:{priority_color};">{priority}</span>'
                    html += '</div>\n'
                    html += f'<p>{content}</p>\n'
                    html += '</div>\n'
            html += '</section>\n'
        
        # 添加实用教程与资源
        tutorials = summary_data.get('tutorials_resources', [])
        if tutorials:
            html += '<section>\n<h2>实用教程与资源分享</h2>\n'
            for tut in tutorials:
                if isinstance(tut, dict):
                    tut_type = tut.get('type', '')
                    title = tut.get('title', '无标题')
                    shared_by = tut.get('sharer', '')
                    tut_time = tut.get('time', '')
                    summary = tut.get('summary', '')
                    key_points = tut.get('key_points', [])
                    
                    html += f'<div class="card">\n'
                    if tut_type:
                        html += f'<div class="dialogue-badge">{tut_type}</div>\n'
                    html += f'<h3>{title}</h3>\n'
                    html += f'<div class="meta">'
                    if shared_by:
                        html += f'<span>分享者：{shared_by}</span> '
                    if tut_time:
                        html += f'<span>时间：{tut_time}</span>'
                    html += '</div>\n'
                    if summary:
                        html += f'<p style="margin-bottom:10px;">{summary}</p>\n'
                    if key_points:
                        html += '<div style="margin-top:12px;"><h4 style="font-size:16px;color:var(--text-secondary);margin-bottom:6px;">要点：</h4><ul>\n'
                        for point in key_points:
                            html += f'<li style="margin-left:20px;margin-bottom:6px;">{point}</li>\n'
                        html += '</ul></div>\n'
                    html += '</div>\n'
            html += '</section>\n'
            
        # 添加问答对
        qa_pairs = summary_data.get('qa_pairs', [])
        if qa_pairs:
            html += '<section>\n<h2>问题与解答</h2>\n'
            for qa in qa_pairs:
                if isinstance(qa, dict):
                    asker = qa.get('asker', '匿名')
                    ask_time = qa.get('ask_time', '未知时间')
                    question = qa.get('question', '无问题描述')
                    tags = qa.get('tags', [])
                    best_answer = qa.get('best_answer', '')
                    answers = qa.get('supplementary_answers', [])
                    
                    html += f'<div class="card">\n'
                    # 问题区域
                    html += f'<div class="qa-question">\n'
                    html += f'  <div class="meta"><span>提问: {asker}</span> <span>@ {ask_time}</span></div>\n'
                    html += f'  <p style="font-weight:500;margin-top:6px;margin-bottom:8px;">{question}</p>\n'
                    
                    if tags:
                        html += '<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:6px;">'
                        for tag in tags:
                            html += f'<span class="tag">{tag}</span>'
                        html += '</div>\n'
                    html += '</div>\n'
                    
                    # 回答区域
                    if best_answer:
                        html += f'<div class="qa-best-answer">\n'
                        html += f'<div class="meta" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">\n'
                        html += f'  <span>最佳回答</span>\n'
                        html += f'  <span class="badge">精选</span>\n'
                        html += f'</div>\n'
                        html += f'<p style="margin:0;">{best_answer}</p>\n'
                        html += '</div>\n'
                    
                    if answers:
                        html += '<div style="margin-top:12px;">\n'
                        if answers and len(answers) > 0:
                            html += f'<div class="meta" style="margin-bottom:8px;">其他回答 ({len(answers)})</div>\n'
                        
                        for i, ans in enumerate(answers):
                            html += f'<div class="qa-answer">\n'
                            html += f'<p style="margin:0;">{ans}</p>\n'
                            html += '</div>\n'
                        html += '</div>\n'
                    
                    html += '</div>\n'
            html += '</section>\n'
        
        # 添加数据分析
        analytics = summary_data.get('data_analysis', {})
        if analytics:
            html += '<section>\n<h2>群内数据可视化</h2>\n'
            
            # 话题热度
            heatmap = analytics.get('topic_heat', [])
            if heatmap:
                html += '<h3>话题热度</h3>\n<div class="heatmap-container">\n'
                for item in heatmap:
                    if isinstance(item, str):
                        # 解析字符串格式 "话题(百分比)|消息数|颜色"
                        parts = item.split('|')
                        if len(parts) >= 3:
                            topic_part = parts[0]  # 包含百分比
                            count_part = parts[1]
                            color = parts[2]
                            
                            # 进一步拆分主题和百分比
                            if '(' in topic_part and ')' in topic_part:
                                topic = topic_part.split('(')[0]
                                percentage = topic_part.split('(')[1].replace(')', '')
                            else:
                                topic = topic_part
                                percentage = 'N/A'
                            
                            html += f'<div class="heat-item">\n'
                            html += f'  <span class="heat-topic">{topic}'
                            if percentage != 'N/A':
                                html += f' ({percentage})'
                            html += f'</span>\n'
                            html += f'  <div class="heat-bar">\n'
                            html += f'    <div class="heat-fill" style="width:{percentage};background-color:{color};"></div>\n'
                            html += f'  </div>\n'
                            html += f'  <span class="heat-count">{count_part}</span>\n'
                            html += f'</div>\n'
                    elif isinstance(item, dict):
                        topic = item.get('topic', '未知话题')
                        percentage = item.get('percentage', 'N/A')
                        count = item.get('count', '?')
                        color = item.get('color', '#cccccc')
                        
                        html += f'<div class="heat-item">\n'
                        html += f'  <span class="heat-topic">{topic}'
                        if percentage != 'N/A':
                            html += f' ({percentage}%)'
                        html += f'</span>\n'
                        html += f'  <div class="heat-bar">\n'
                        html += f'    <div class="heat-fill" style="width:{percentage}%;background-color:{color};"></div>\n'
                        html += f'  </div>\n'
                        html += f'  <span class="heat-count">{count}条</span>\n'
                        html += f'</div>\n'
                html += '</div>\n'
                
            # 话唠榜
            talkers = analytics.get('top_chatters', [])
            if talkers:
                html += '<h3>话唠榜</h3>\n'
                html += '<div class="participants-container">\n'
                for p in talkers:
                    if isinstance(p, dict):
                        rank = p.get('rank', '#')
                        name = p.get('nickname', '匿名')
                        msg_count = p.get('message_count', '0')
                        profile = p.get('user_profile', '')
                        keywords = p.get('frequent_words', [])
                        
                        html += f'<div class="participant-item">\n'
                        html += f'  <div class="participant-rank">{rank}</div>\n'
                        html += f'  <div class="participant-info">\n'
                        html += f'    <div class="participant-name">{name}</div>\n'
                        html += f'    <div class="participant-count">{msg_count} 条消息</div>\n'
                        if profile:
                            html += f'    <div style="font-style:italic;color:var(--text-secondary);margin-bottom:8px;font-size:13px;">{profile}</div>\n'
                        if keywords:
                            html += f'    <div style="display:flex;flex-wrap:wrap;gap:6px;">\n'
                            html += f'      <span style="color:var(--text-secondary);">常用词:</span>\n'
                            for kw in keywords:
                                html += f'      <span class="keyword" style="font-size:12px;padding:2px 8px;">{kw}</span>\n'
                            html += f'    </div>\n'
                        html += f'  </div>\n'
                        html += f'</div>\n'
                html += '</div>\n'
            
            # 熬夜冠军
            night_owl = analytics.get('night_owl', {})
            if night_owl:
                name = night_owl.get('nickname', '匿名')
                active_time = night_owl.get('latest_active_time', '未知')  # 重命名变量避免冲突
                msg_count = night_owl.get('late_night_messages', '0')
                title = night_owl.get('title', '')
                message = night_owl.get('representative_message', '')
                
                html += '<h3>熬夜冠军</h3>\n'
                html += '<div class="night-owl-item">\n'
                html += f'  <div>\n'
                html += f'    <span class="owl-crown" title="熬夜冠军">👑</span>\n'
                html += f'    <span style="font-weight:600;font-size:16px;">{name}</span>\n'
                html += f'  </div>\n'
                if title:
                    html += f'  <div style="color:var(--accent-primary);font-style:italic;margin-bottom:6px;font-size:14px;">{title}</div>\n'
                html += f'  <div style="color:var(--text-secondary);margin-bottom:4px;font-size:13px;">最晚活跃时间：{active_time}</div>\n'
                html += f'  <div style="color:var(--text-secondary);margin-bottom:4px;font-size:13px;">深夜消息数：{msg_count}条</div>\n'
                if message:
                    html += f'  <div style="font-size:13px;color:var(--text-secondary);margin-top:6px;font-style:italic;">代表性消息: "{message}"</div>\n'
                html += '</div>\n'
                
            html += '</section>\n'
        
        # 添加词云
        word_cloud = summary_data.get('word_cloud', [])
        if word_cloud:
            html += '<section>\n<h2>热门词云</h2>\n<div class="word-cloud-container">\n'
            for word in word_cloud:
                if isinstance(word, dict):
                    text = word.get('word', word.get('text', ''))
                    size = word.get('size', word.get('size', 16))
                    color = word.get('color', word.get('color', '#fffffe'))
                    if text:
                        html += f'<span class="cloud-word" style="font-size:{size}px;color:{color};">{text}</span>\n'
            html += '</div>\n</section>\n'
        
        # 添加趣味内容
        fun_content = summary_data.get('fun_content', [])
        if fun_content:
            html += '<section>\n<h2>有趣对话或金句</h2>\n'
            for content in fun_content:
                if isinstance(content, dict):
                    content_type = content.get('type', '')
                    dialogues = content.get('dialogue', [])
                    dialogue_time = content.get('time', '')
                    highlight = content.get('highlight', '')
                    related_topic = content.get('related_topic', '')
                    
                    html += f'<div class="card">\n'
                    if content_type:
                        html += f'<div class="dialogue-badge">{content_type}</div>\n'
                    
                    if dialogues:
                        html += f'<div class="dialogue-content">\n'
                        for msg in dialogues:
                            html += f'<p>{msg}</p>\n'
                        html += '</div>\n'
                        
                    if highlight:
                        html += f'<div class="dialogue-highlight">金句：{highlight}</div>\n'
                        
                    if related_topic:
                        html += f'<div style="font-size:13px;color:var(--text-secondary);margin-top:10px;">相关话题：{related_topic}</div>\n'
                        
                    html += '</div>\n'
            html += '</section>\n'
        
        # 添加页脚 - 使用正确的time_module
        generation_time = time_module.strftime("%Y-%m-%d %H:%M:%S")
        html += f'''<footer>
    <p>数据来源：{group_name}聊天记录 | 生成时间：{generation_time}</p>
    <p>统计周期：{date} {time_range}</p>
</footer>
</body>
</html>'''
        
        return html
    except Exception as e:
        logger.error(f"生成极简HTML时出错: {e}", exc_info=True)
        # 在异常处理中也使用不同的模块名避免冲突
        import time as time_module 
        current_time = time_module.strftime("%Y-%m-%d %H:%M:%S")
        return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>聊天记录总结</title>
    <style>
        body {{ 
            background:#0f0e17; 
            color:#fffffe; 
            font-family: 'SF Pro Display', 'Segoe UI', 'Roboto', 'PingFang SC', 'Microsoft YaHei', '微软雅黑', sans-serif;
            padding:20px; 
        }}
        h1 {{ color:#ff8906; }}
        p {{ line-height:1.6; }}
    </style>
</head>
<body>
    <h1>生成群聊总结</h1>
    <p>生成时间: {current_time}</p>
    <p>抱歉，在生成详细HTML时遇到了问题: {e}</p>
    <p>请检查日志获取更多信息。以下是基本摘要:</p>
    <div style="background:#1a1925; padding:18px; border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.15);">
        <p>日期: {summary_data.get('metadata', {}).get('date', '未知')}</p>
        <p>消息数: {summary_data.get('metadata', {}).get('total_messages', 'N/A')}</p>
        <p>热点话题数: {len(summary_data.get('hot_topics', []))}个</p>
    </div>
</body>
</html>"""

def generate_summary_image_from_data(summary_data: dict, output_dir: str = str(DEFAULT_OUTPUT_DIR), is_group_chat: bool = False) -> str | None:
    """
    协调函数：根据 JSON 数据生成 HTML 并将其渲染为图片。
    增强版：优先尝试标准模板，失败后根据场景回退。

    Args:
        summary_data: 从 LLM 获取并解析后的 JSON 数据字典。
        output_dir: 图片输出目录。
        is_group_chat: 是否为群聊场景。

    Returns:
        成功时返回图片文件路径，失败返回 None。
        失败时会设置全局变量 last_text_summary 作为文本备选方案。
    """
    global last_text_summary
    last_text_summary = None
    
    # 添加日志: 记录传入的原始元数据 (如果存在)
    original_metadata = summary_data.get('metadata', '元数据字段不存在')
    logger.info(f"[图片总结] 接收到的原始元数据: {original_metadata}")
    
    try:
        # 先检查依赖
        check_dependencies()
        
        # 对数据进行清理和限制 (保持一定的限制)
        max_items = 10 # 统一标准，让 Playwright 尝试处理
        max_text_length = 500 # 统一标准
        if is_group_chat:
            logger.info("[图片总结] 群聊模式，但优先尝试标准模板渲染")
            # 可以考虑稍微降低群聊的限制，即使是标准模板
            # max_items = 8
            # max_text_length = 400
            # cleaned_data = sanitize_summary_data(summary_data, max_items, max_text_length)
            cleaned_data = summary_data # 暂时保持和私聊一致的数据量进行尝试
        else:
            logger.info("[图片总结] 私聊模式，使用标准模板渲染")
            cleaned_data = summary_data

        # --- 步骤 1: 始终生成标准 HTML --- 
        html_content = ""
        try:
            html_content = generate_summary_html(cleaned_data)
            logger.info("[图片总结] 已生成标准 HTML 模板内容")
        except Exception as html_gen_error:
             logger.error(f"[图片总结] 生成标准 HTML 时出错: {html_gen_error}", exc_info=True)
             last_text_summary = generate_text_summary(summary_data)
             return None # HTML 生成失败，直接回退

        # 使用带时间戳的文件名避免冲突
        timestamp = int(time.time())
        output_filename_base = f"summary_{timestamp}"
        output_png_path = os.path.join(output_dir, f"{output_filename_base}.png")

        # 保存中间 HTML (标准版)
        html_filename = f"debug_{output_filename_base}_standard.html"
        html_filepath = os.path.join(output_dir, html_filename)
        try:
            with open(html_filepath, "w", encoding="utf-8") as f:
                f.write(html_content)
            logger.info(f"Saved intermediate standard HTML to: {html_filepath}")
        except Exception as e:
            logger.warning(f"Failed to save intermediate standard HTML: {e}")
        
        # --- 步骤 2: 优先尝试 Playwright 渲染标准 HTML --- 
        playwright_success = False
        try:
            # 可以在这里根据 is_group_chat 稍微调整 Playwright 参数，如内存限制
            # if is_group_chat:
            #     os.environ["PLAYWRIGHT_BROWSER_MEM_LIMIT"] = "768"
            # else:
            #     os.environ["PLAYWRIGHT_BROWSER_MEM_LIMIT"] = "1024"
            playwright_success = render_html_to_image(html_content, output_png_path)
        except Exception as pw_render_error:
            logger.error(f"[图片总结] 使用 Playwright 渲染标准 HTML 时发生意外错误: {pw_render_error}", exc_info=True)
            playwright_success = False # 确保标记为失败

        # --- 步骤 3: 处理渲染结果 --- 
        if playwright_success:
            logger.info(f"[图片总结] 标准模板使用 Playwright 渲染成功: {output_png_path}")
            return output_png_path
        else:
            logger.warning("[图片总结] 标准模板使用 Playwright 渲染失败，开始尝试回退...")
            
            # --- 步骤 4: 回退逻辑 --- 
            fallback_success = False
            if is_group_chat:
                # --- 群聊回退: 尝试极简模板 + wkhtmltopdf --- 
                logger.info("[图片总结] 群聊场景：标准渲染失败，尝试生成并渲染极简模板...")
                lite_html_content = ""
                try:
                    lite_html_content = generate_lite_html(cleaned_data)
                    # 保存极简 HTML 供调试
                    lite_html_filename = f"debug_{output_filename_base}_lite.html"
                    lite_html_filepath = os.path.join(output_dir, lite_html_filename)
                    with open(lite_html_filepath, "w", encoding="utf-8") as f:
                        f.write(lite_html_content)
                    logger.info(f"Saved intermediate lite HTML to: {lite_html_filepath}")
                except Exception as lite_gen_error:
                     logger.error(f"[图片总结] 生成极简 HTML 时出错: {lite_gen_error}", exc_info=True)
                     # 极简 HTML 生成失败，直接跳到文本回退
                     last_text_summary = generate_text_summary(summary_data)
                     return None
                
                # 尝试用 wkhtmltopdf 渲染极简模板
                logger.info("[图片总结] 尝试使用 wkhtmltopdf 渲染极简模板...")
                fallback_success = render_with_wkhtmltopdf(lite_html_content, output_png_path)
                if fallback_success:
                     logger.info(f"[图片总结] 群聊场景：极简模板使用 wkhtmltopdf 渲染成功: {output_png_path}")
                else:
                     logger.warning("[图片总结] 群聊场景：wkhtmltopdf 渲染极简模板也失败。")
                     
            else:
                # --- 私聊回退: 尝试标准模板 + wkhtmltopdf --- 
                logger.info("[图片总结] 私聊场景：标准渲染失败，尝试使用 wkhtmltopdf 渲染标准模板...")
                fallback_success = render_with_wkhtmltopdf(html_content, output_png_path)
                if fallback_success:
                     logger.info(f"[图片总结] 私聊场景：标准模板使用 wkhtmltopdf 渲染成功: {output_png_path}")
                else:
                     logger.warning("[图片总结] 私聊场景：wkhtmltopdf 渲染标准模板也失败。")
            
            # --- 步骤 5: 最终处理 --- 
            if fallback_success and os.path.exists(output_png_path):
                 return output_png_path # 备选渲染成功
            else:
                 logger.error("[图片总结] 所有图片渲染方法均失败，生成文本摘要作为最终回退。")
                 last_text_summary = generate_text_summary(summary_data)
                 logger.info("已生成文本摘要作为备选方案")
                 return None

    except ImportError as e: # 捕获 check_dependencies 的异常
         logger.error(f"依赖项检查失败: {e}")
         # 生成文本摘要
         last_text_summary = generate_text_summary(summary_data)
         logger.info("已生成文本摘要作为备选方案")
         # 可以在这里决定是否抛出异常，以便上层知道是依赖问题
         # raise e 
         return None # 或者仅返回 None
    except Exception as e:
        logger.error(f"执行图片总结渲染时发生未知错误: {e}", exc_info=True)
        # 生成文本摘要
        last_text_summary = generate_text_summary(summary_data)
        logger.info("已生成文本摘要作为备选方案")
        return None

# 全局变量，用于存储最后生成的文本摘要，作为渲染失败的备选方案
last_text_summary = None

def get_last_text_summary():
    """获取最后生成的文本摘要"""
    return last_text_summary

# --- 图片数据处理辅助函数 ---
def get_image_data_uri(image_path_or_data: str | bytes, expected_mime_type: str | None = None) -> str | None:
    """将图片路径或字节数据转换为 Data URI。尝试确定 MIME 类型。"""
    try:
        mime_type = None
        img_data = None

        if isinstance(image_path_or_data, bytes):
            img_data = image_path_or_data
            # 尝试从数据头部猜测 MIME 类型 (需要安装 python-magic 或 Pillow)
            # 简单实现：优先使用传入的类型，否则猜测常见类型
            if expected_mime_type:
                mime_type = expected_mime_type
            elif img_data.startswith(b'\x89PNG\r\n\x1a\n'):
                mime_type = "image/png"
            elif img_data.startswith(b'\xff\xd8\xff'):
                mime_type = "image/jpeg"
            elif img_data.startswith(b'GIF8'):
                mime_type = "image/gif"
            else:
                 # 默认或回退
                 mime_type = "image/png"
                 logger.debug("无法从字节数据确定 MIME 类型，默认为 image/png")

        elif isinstance(image_path_or_data, str) and os.path.exists(image_path_or_data):
            file_path = Path(image_path_or_data)
            suffix = file_path.suffix.lstrip('.').lower()
            # 基本的基于后缀的 MIME 类型判断
            if suffix in ["jpg", "jpeg"]:
                mime_type = "image/jpeg"
            elif suffix == "png":
                mime_type = "image/png"
            elif suffix == "gif":
                mime_type = "image/gif"
            elif suffix == "webp":
                mime_type = "image/webp"
            else:
                 # 尝试使用传入的类型或默认
                mime_type = expected_mime_type if expected_mime_type else "application/octet-stream"
                logger.warning(f"无法根据后缀 {suffix} 确定图片 MIME 类型，使用 {mime_type}")

            with open(file_path, "rb") as image_file:
                img_data = image_file.read()
        else:
            logger.warning(f"无法处理的图片数据类型或路径不存在: {image_path_or_data}")
            return None

        if img_data and mime_type:
            base64_data = base64.b64encode(img_data).decode('utf-8')
            return f"data:{mime_type};base64,{base64_data}"
        else:
            return None

    except Exception as e:
        logger.error(f"转换图片为 Data URI 时出错: {e}", exc_info=True)
        return None