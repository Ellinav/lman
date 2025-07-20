# modules/image_generation.py

import asyncio
import json
import re
import time
import uuid
import random
from typing import AsyncGenerator

# 全局变量，之后会从主服务传入
logger = None
response_channels = None
CONFIG = None
MODEL_NAME_TO_ID_MAP = None # 虽然文生图不常用，但保留以备将来使用
DEFAULT_MODEL_ID = None
MODEL_ENDPOINT_MAP = None # 【【【新增】】】 接收“个人通讯录”


def initialize_image_module(app_logger, channels, app_config, model_map, default_model_id, model_endpoint_map):
    """初始化模块所需的全局变量。"""
    global logger, response_channels, CONFIG, MODEL_NAME_TO_ID_MAP, DEFAULT_MODEL_ID, MODEL_ENDPOINT_MAP
    logger = app_logger
    response_channels = channels
    CONFIG = app_config
    MODEL_NAME_TO_ID_MAP = model_map
    DEFAULT_MODEL_ID = default_model_id
    MODEL_ENDPOINT_MAP = model_endpoint_map # 【【【新增】】】 保存“个人通讯录”的引用
    logger.info("文生图模块已成功初始化 (v2)。")

def convert_to_lmarena_image_payload(prompt: str, model_id: str, session_id: str, message_id: str) -> dict:
    # ... 此函数无需修改 ...
    """将文本提示转换为 LMArena 图片生成的载荷。"""
    return {
        "is_image_request": True,
        "message_templates": [{
            "role": "user",
            "content": prompt,
            "attachments": [],
            "participantPosition": "a"
        }],
        "target_model_id": model_id,
        "session_id": session_id,
        "message_id": message_id
    }

async def _process_image_stream(request_id: str) -> AsyncGenerator[tuple[str, str], None]:
    # ... 此函数无需修改 ...
    """处理来自浏览器的图片生成数据流，并产生结构化事件。"""
    queue = response_channels.get(request_id)
    if not queue:
        logger.error(f"IMAGE PROCESSOR [ID: {request_id[:8]}]: 无法找到响应通道。")
        yield 'error', 'Internal server error: response channel not found.'
        return

    buffer = ""
    timeout = CONFIG.get("stream_response_timeout_seconds", 360)
    image_pattern = re.compile(r'[ab]2:(\[.*?\])')
    finish_pattern = re.compile(r'[ab]d:(\{.*?"finishReason".*?\})')
    error_pattern = re.compile(r'(\{\s*".*?"\s*:\s*".*?"(error|context_file).*?"\s*\})', re.DOTALL | re.IGNORECASE)
    
    found_image_url = None

    try:
        while True:
            try:
                raw_data = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                if found_image_url:
                    yield 'image_url', found_image_url
                else:
                    yield 'error', f'Response timed out after {timeout} seconds.'
                return

            if isinstance(raw_data, dict) and 'error' in raw_data:
                yield 'error', raw_data.get('error', 'Unknown browser error')
                return
            
            if raw_data == "[DONE]":
                break

            buffer += "".join(str(item) for item in raw_data) if isinstance(raw_data, list) else raw_data
            
            if (error_match := error_pattern.search(buffer)):
                try:
                    error_json = json.loads(error_match.group(1))
                    yield 'error', error_json.get("error", "来自 LMArena 的未知错误")
                    return
                except json.JSONDecodeError: pass

            if not found_image_url:
                while (match := image_pattern.search(buffer)):
                    try:
                        image_data_list = json.loads(match.group(1))
                        if isinstance(image_data_list, list) and image_data_list:
                            image_info = image_data_list[0]
                            if image_info.get("type") == "image" and "image" in image_info:
                                found_image_url = image_info["image"]
                                buffer = buffer[match.end():]
                                break
                    except (json.JSONDecodeError, IndexError) as e:
                        logger.error(f"解析图片URL时出错: {e}, buffer: {buffer}")
                    buffer = buffer[match.end():]

            if (finish_match := finish_pattern.search(buffer)):
                try:
                    finish_data = json.loads(finish_match.group(1))
                    yield 'finish', finish_data.get("finishReason", "stop")
                except (json.JSONDecodeError, IndexError): pass
                buffer = buffer[finish_match.end():]
        
        if found_image_url:
            yield 'image_url', found_image_url
        elif not any(e[0] == 'finish' for e in locals().get('_debug_events', [])):
            yield 'error', 'Stream ended without providing an image URL.'

    finally:
        if request_id in response_channels:
            del response_channels[request_id]

async def generate_single_image(prompt: str, model_name: str, browser_ws) -> str | dict:
    """
    【【【核心重构】】】
    执行单次文生图请求，并返回图片 URL 或错误字典。
    现在它会从 MODEL_ENDPOINT_MAP 中为指定模型查找并随机选择一个ID。
    """
    if not browser_ws:
        return {"error": "Browser client not connected."}

    # 1. 从“个人通讯录”中查找为该模型捕获的ID
    if not model_name or model_name not in MODEL_ENDPOINT_MAP:
        return {"error": f"Model '{model_name}' not found in captured endpoints. Please capture an ID for this image model first."}

    mapping_entry = MODEL_ENDPOINT_MAP[model_name]
    selected_mapping = random.choice(mapping_entry) if isinstance(mapping_entry, list) and mapping_entry else mapping_entry
    
    if not selected_mapping:
        return {"error": f"No valid endpoint entries found for model '{model_name}'."}

    session_id = selected_mapping.get("sessionId")
    message_id = selected_mapping.get("messageId")
    # 文生图请求的 model_id 使用默认值即可，session_id 是关键
    target_model_id = DEFAULT_MODEL_ID

    if not session_id or not message_id:
        return {"error": f"Captured endpoint for '{model_name}' is missing session_id or message_id."}

    request_id = str(uuid.uuid4())
    response_channels[request_id] = asyncio.Queue()

    try:
        lmarena_payload = convert_to_lmarena_image_payload(prompt, target_model_id, session_id, message_id)
        message_to_browser = {"request_id": request_id, "payload": lmarena_payload}
        
        logger.info(f"IMAGE GEN [ID: {request_id[:8]}][Model: {model_name}]: 正在发送请求...")
        await browser_ws.send_text(json.dumps(message_to_browser))

        async for event_type, data in _process_image_stream(request_id):
            if event_type == 'image_url':
                logger.info(f"IMAGE GEN [ID: {request_id[:8]}]: 成功获取图片 URL。")
                return data
            elif event_type == 'error':
                 logger.error(f"IMAGE GEN [ID: {request_id[:8]}]: 流处理错误: {data}")
                 return {"error": data}
            elif event_type == 'finish' and data == 'content-filter':
                return {"error": "响应被内容过滤器终止。"}
        
        return {"error": "Image generation stream ended without a result."}

    except Exception as e:
        logger.error(f"IMAGE GEN [ID: {request_id[:8]}]: 处理时发生致命错误: {e}", exc_info=True)
        if request_id in response_channels:
            del response_channels[request_id]
        return {"error": "An internal server error occurred."}


async def handle_image_generation_request(request, browser_ws):
    # ... 此函数无需修改 ...
    """处理文生图API端点请求，支持并行生成。"""
    try:
        req_body = await request.json()
    except json.JSONDecodeError:
        return {"error": "Invalid JSON request body"}, 400

    prompt = req_body.get("prompt")
    if not prompt:
        return {"error": "Prompt is required"}, 400
    
    n = req_body.get("n", 1)
    if not isinstance(n, int) or not 1 <= n <= 10:
        return {"error": "Parameter 'n' must be an integer between 1 and 10."}, 400

    model_name = req_body.get("model", "dall-e-3") # 客户端请求的模型名
    logger.info(f"收到文生图请求: n={n}, model='{model_name}', prompt='{prompt[:30]}...'")

    tasks = [generate_single_image(prompt, model_name, browser_ws) for _ in range(n)]
    results = await asyncio.gather(*tasks)

    successful_urls = [res for res in results if isinstance(res, str)]
    errors = [res['error'] for res in results if isinstance(res, dict)]

    if errors:
        logger.error(f"文生图请求中有 {len(errors)} 个任务失败: {errors}")
    
    if not successful_urls:
        error_message = f"All {n} image generation tasks failed. Last error: {errors[-1] if errors else 'Unknown error'}"
        return {"error": error_message}, 500

    response_data = {
        "created": int(time.time()),
        "data": [{"url": url} for url in successful_urls]
    }
    return response_data, 200
