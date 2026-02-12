from fastapi import FastAPI, HTTPException, Request, status, Security, Header
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
import httpx
import asyncio
import logging
import json
import time
import hashlib
from typing import Optional, Dict, Any
from enum import Enum
from collections import defaultdict
from datetime import datetime, timedelta
import uuid

# 配置日志 - 移除敏感信息
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 🔒 安全配置
ENABLE_ACCESS_TOKEN = False  # 设置为 True 启用访问令牌验证
ACCESS_TOKEN = "your-secret-token-here"  # 修改为你的访问令牌

# 🔒 速率限制配置
RATE_LIMIT_REQUESTS = 20  # 每个 IP 每分钟最多请求数
RATE_LIMIT_WINDOW = 60  # 时间窗口（秒）

# 🔒 请求大小限制
MAX_MESSAGE_LENGTH = 10000  # 单条消息最大字符数
MAX_MESSAGES_COUNT = 50  # 最大消息数量
MAX_TOKENS = 8000  # 最大 token 数

app = FastAPI(
    title="Grok & Gemini API Proxy (Secured)",
    description="高性能安全代理接口，OpenAI 兼容格式",
    version="2.3.0",
    docs_url=None if ENABLE_ACCESS_TOKEN else "/docs",
    redoc_url=None if ENABLE_ACCESS_TOKEN else "/redoc"
)

# 🔒 CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应改为具体域名
    allow_credentials=True,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# API 基础 URL 配置
API_BASES = {
    "grok": "https://api.x.ai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta"
}

# 🔒 速率限制存储（内存）
rate_limit_storage: Dict[str, list] = defaultdict(list)

# 🔒 允许的模型列表（白名单）- 严格按照用户要求
ALLOWED_MODELS = {
    "grok": ["grok-4-1-fast-reasoning", "grok-4-1-fast-non-reasoning", "grok-4-0709"],
    "gemini": ["gemini-3-flash-preview", "gemini-3-pro-preview", "gemini-2.5-flash-lite"]
}

class Provider(str, Enum):
    GROK = "grok"
    GEMINI = "gemini"

# 创建共享的 HTTP 客户端
http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(60.0, connect=10.0),
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
    http2=True
)

class ChatRequest(BaseModel):
    messages: list[dict] = Field(..., description="消息列表")
    model: str = Field(..., description="模型ID")
    max_tokens: Optional[int] = Field(None, ge=1, le=MAX_TOKENS, description="最大令牌数")
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0, description="温度参数")
    top_p: Optional[float] = Field(None, ge=0.0, le=1.0, description="Top-p采样")
    stream: bool = Field(default=False, description="是否流式输出")
    presence_penalty: Optional[float] = Field(None, ge=-2.0, le=2.0)
    frequency_penalty: Optional[float] = Field(None, ge=-2.0, le=2.0)
    provider: Optional[str] = Field(None, description="指定API提供商")

    @validator('messages')
    def validate_messages(cls, v):
        """验证消息列表"""
        if not v:
            raise ValueError("消息列表不能为空")
        
        if len(v) > MAX_MESSAGES_COUNT:
            raise ValueError(f"消息数量不能超过 {MAX_MESSAGES_COUNT}")
        
        for msg in v:
            if 'role' not in msg or 'content' not in msg:
                raise ValueError("每条消息必须包含 role 和 content")
            
            if msg['role'] not in ['system', 'user', 'assistant']:
                raise ValueError(f"无效的角色: {msg['role']}")
            
            if len(str(msg['content'])) > MAX_MESSAGE_LENGTH:
                raise ValueError(f"单条消息不能超过 {MAX_MESSAGE_LENGTH} 字符")
        
        return v
    
    @validator('model')
    def validate_model(cls, v):
        """验证模型名称"""
        # 检查是否在允许的模型列表中
        for provider_models in ALLOWED_MODELS.values():
            if v in provider_models:
                return v
        
        raise ValueError(f"不支持的模型: {v}")

def mask_api_key(api_key: str) -> str:
    """🔒 脱敏 API Key"""
    if not api_key or len(api_key) < 8:
        return "***"
    return f"{api_key[:4]}...{api_key[-4:]}"

def get_client_ip(request: Request) -> str:
    """获取客户端真实 IP"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip
    
    return request.client.host if request.client else "unknown"

def check_rate_limit(ip: str) -> bool:
    """🔒 检查速率限制"""
    now = time.time()
    
    # 清理过期记录
    rate_limit_storage[ip] = [
        timestamp for timestamp in rate_limit_storage[ip]
        if now - timestamp < RATE_LIMIT_WINDOW
    ]
    
    # 检查是否超限
    if len(rate_limit_storage[ip]) >= RATE_LIMIT_REQUESTS:
        return False
    
    # 记录本次请求
    rate_limit_storage[ip].append(now)
    return True

async def verify_access_token(authorization: Optional[str] = Header(None)) -> bool:
    """🔒 验证访问令牌（可选）"""
    if not ENABLE_ACCESS_TOKEN:
        return True
    
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要访问令牌"
        )
    
    token = authorization.replace("Bearer ", "")
    if token != ACCESS_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="访问令牌无效"
        )
    
    return True

def detect_provider_from_model(model: str) -> str:
    """根据模型名称自动检测提供商"""
    for provider, models in ALLOWED_MODELS.items():
        if model in models:
            return provider
    
    # 后备检测
    model_lower = model.lower()
    if any(x in model_lower for x in ["grok"]):
        return "grok"
    elif any(x in model_lower for x in ["gemini"]):
        return "gemini"
    
    raise ValueError(f"无法识别的模型: {model}")

def convert_gemini_to_openai(gemini_response: dict, model: str) -> dict:
    """
    🔄 将 Gemini 响应转换为 OpenAI 格式（非流式）
    """
    try:
        # Gemini 响应格式示例：
        # {
        #   "candidates": [{
        #     "content": {
        #       "parts": [{"text": "..."}],
        #       "role": "model"
        #     },
        #     "finishReason": "STOP"
        #   }],
        #   "usageMetadata": {...}
        # }
        
        if "candidates" not in gemini_response or not gemini_response["candidates"]:
            # 如果没有candidates，返回空响应
            return {
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": ""
                    },
                    "finish_reason": "stop"
                }],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                }
            }
        
        candidate = gemini_response["candidates"][0]
        content_parts = candidate.get("content", {}).get("parts", [])
        
        # 合并所有 text parts
        text_content = "".join(part.get("text", "") for part in content_parts)
        
        # 转换 finishReason
        finish_reason_map = {
            "STOP": "stop",
            "MAX_TOKENS": "length",
            "SAFETY": "content_filter",
            "RECITATION": "content_filter",
            "OTHER": "stop"
        }
        finish_reason = finish_reason_map.get(
            candidate.get("finishReason", "STOP"), 
            "stop"
        )
        
        # 提取 usage 信息
        usage_metadata = gemini_response.get("usageMetadata", {})
        
        # 构造 OpenAI 格式响应
        openai_response = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text_content
                },
                "finish_reason": finish_reason
            }],
            "usage": {
                "prompt_tokens": usage_metadata.get("promptTokenCount", 0),
                "completion_tokens": usage_metadata.get("candidatesTokenCount", 0),
                "total_tokens": usage_metadata.get("totalTokenCount", 0)
            }
        }
        
        return openai_response
        
    except Exception as e:
        logger.error(f"Gemini 响应转换失败: {type(e).__name__}")
        # 返回一个基础的 OpenAI 格式响应
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "响应转换错误"
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
        }

def convert_gemini_stream_chunk_to_openai(gemini_chunk: dict, model: str) -> Optional[dict]:
    """
    🔄 将 Gemini 流式响应块转换为 OpenAI 格式
    """
    try:
        # Gemini 流式响应格式：
        # {
        #   "candidates": [{
        #     "content": {
        #       "parts": [{"text": "..."}],
        #       "role": "model"
        #     }
        #   }]
        # }
        
        if "candidates" not in gemini_chunk or not gemini_chunk["candidates"]:
            return None
        
        candidate = gemini_chunk["candidates"][0]
        content_parts = candidate.get("content", {}).get("parts", [])
        
        # 提取文本内容
        text_content = "".join(part.get("text", "") for part in content_parts)
        
        if not text_content:
            return None
        
        # 检查是否是结束块
        finish_reason = None
        if "finishReason" in candidate:
            finish_reason_map = {
                "STOP": "stop",
                "MAX_TOKENS": "length",
                "SAFETY": "content_filter",
                "RECITATION": "content_filter",
                "OTHER": "stop"
            }
            finish_reason = finish_reason_map.get(candidate["finishReason"], "stop")
        
        # 构造 OpenAI 流式格式
        openai_chunk = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {
                    "content": text_content
                } if text_content else {},
                "finish_reason": finish_reason
            }]
        }
        
        return openai_chunk
        
    except Exception as e:
        logger.error(f"Gemini 流式块转换失败: {type(e).__name__}")
        return None

def build_gemini_payload(req: ChatRequest) -> Dict[str, Any]:
    """构建 Gemini 请求负载"""
    contents = []
    for msg in req.messages:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append({
            "role": role,
            "parts": [{"text": msg["content"]}]
        })
    
    payload = {"contents": contents}
    
    generation_config = {}
    if req.temperature is not None:
        generation_config["temperature"] = req.temperature
    if req.max_tokens is not None:
        generation_config["maxOutputTokens"] = req.max_tokens
    if req.top_p is not None:
        generation_config["topP"] = req.top_p
    
    if generation_config:
        payload["generationConfig"] = generation_config
    
    return payload

def build_grok_payload(req: ChatRequest) -> Dict[str, Any]:
    """构建 Grok 请求负载"""
    payload = {
        "model": req.model,
        "messages": req.messages,
        "stream": req.stream
    }
    
    if req.max_tokens is not None:
        payload["max_tokens"] = req.max_tokens
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.top_p is not None:
        payload["top_p"] = req.top_p
    if req.presence_penalty is not None:
        payload["presence_penalty"] = req.presence_penalty
    if req.frequency_penalty is not None:
        payload["frequency_penalty"] = req.frequency_penalty
    
    return payload

def build_headers(provider: str, api_key: str, stream: bool = False) -> Dict[str, str]:
    """构建请求头"""
    if provider == "gemini":
        return {"Content-Type": "application/json"}
    else:  # grok
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json"
        }

def build_url(provider: str, endpoint: str, api_key: str = None, model: str = None) -> str:
    """构建请求 URL"""
    base_url = API_BASES[provider]
    
    if provider == "gemini":
        if endpoint == "chat":
            return f"{base_url}/models/{model}:generateContent?key={api_key}"
        elif endpoint == "stream":
            return f"{base_url}/models/{model}:streamGenerateContent?key={api_key}&alt=sse"
        else:
            return f"{base_url}/models?key={api_key}"
    else:
        if endpoint == "chat":
            return f"{base_url}/chat/completions"
        else:
            return f"{base_url}/models"

async def stream_gemini_response(response, model: str):
    """
    🔄 Gemini 流式响应处理 - 转换为 OpenAI 格式
    """
    try:
        async for line in response.aiter_lines():
            if line:
                if line.startswith("data: "):
                    data_str = line[6:].strip()
                    
                    # 跳过空数据
                    if not data_str:
                        continue
                    
                    try:
                        # 解析 Gemini 的 JSON 数据
                        gemini_data = json.loads(data_str)
                        
                        # 转换为 OpenAI 格式
                        openai_chunk = convert_gemini_stream_chunk_to_openai(gemini_data, model)
                        
                        if openai_chunk:
                            # 按照 SSE 标准格式输出：data: {json}\n\n
                            yield f"data: {json.dumps(openai_chunk)}\n\n"
                        
                    except json.JSONDecodeError:
                        # 如果不是 JSON，跳过
                        continue
        
        # 发送结束标记（OpenAI 格式）
        yield "data: [DONE]\n\n"
        
    except Exception as e:
        logger.error(f"Gemini 流式传输错误: {type(e).__name__}")
        error_chunk = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop"
            }]
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"
        yield "data: [DONE]\n\n"

async def stream_grok_response(response):
    """
    📡 Grok 流式响应处理 - 确保符合 SSE 标准格式
    """
    try:
        async for line in response.aiter_lines():
            if line:
                # Grok 已经是 OpenAI 格式，但需要确保格式规范
                if line.startswith("data: "):
                    # 按照 SSE 标准：每个数据块后面必须有两个换行符
                    yield f"{line}\n\n"
                else:
                    # 如果不是标准格式，修正它
                    yield f"data: {line}\n\n"
                    
    except Exception as e:
        logger.error(f"Grok 流式传输错误: {type(e).__name__}")
        error_data = json.dumps({'error': '流式传输错误'})
        yield f"data: {error_data}\n\n"

@app.post("/v1/chat/completions")
@app.post("/{provider}/v1/chat/completions")
async def chat_completions(
    req: ChatRequest,
    request: Request,
    provider: str = None,
    authorization: Optional[str] = Header(None)
):
    """聊天完成接口 - OpenAI 兼容格式"""
    
    # 🔒 访问令牌验证（如果启用）
    if ENABLE_ACCESS_TOKEN:
        await verify_access_token(authorization)
    
    # 🔒 速率限制检查
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip):
        logger.warning(f"速率限制触发: IP={client_ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"请求过于频繁，请在 {RATE_LIMIT_WINDOW} 秒后重试"
        )
    
    # 检测提供商
    if provider is None:
        provider = req.provider or detect_provider_from_model(req.model)
    
    provider = provider.lower()
    
    if provider not in API_BASES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的提供商: {provider}"
        )
    
    # 🔒 验证模型名称
    if req.model not in ALLOWED_MODELS.get(provider, []):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"模型 {req.model} 不支持提供商 {provider}"
        )
    
    # 获取 API Key
    api_key = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not api_key and provider == "gemini":
        api_key = request.headers.get("x-api-key", "")
    
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少API密钥"
        )
    
    # 🔒 记录请求（脱敏）
    logger.info(
        f"请求: IP={client_ip} Provider={provider} Model={req.model} "
        f"Stream={req.stream} APIKey={mask_api_key(api_key)}"
    )

    try:
        # 构建请求
        headers = build_headers(provider, api_key, req.stream)
        
        if provider == "gemini":
            payload = build_gemini_payload(req)
            if req.stream:
                url = build_url(provider, "stream", api_key, req.model)
            else:
                url = build_url(provider, "chat", api_key, req.model)
        else:
            payload = build_grok_payload(req)
            url = build_url(provider, "chat", api_key)
        
        # 🔒 不记录完整 URL（Gemini URL 包含 API key）
        if provider == "gemini":
            logger.debug(f"请求 Gemini API")
        else:
            logger.debug(f"请求 {provider} API")
        
        # 发送请求
        response = await http_client.post(
            url,
            headers=headers,
            json=payload,
            timeout=60.0
        )
        response.raise_for_status()

        # 返回响应
        if req.stream:
            if provider == "gemini":
                # Gemini 流式 → OpenAI 格式
                return StreamingResponse(
                    stream_gemini_response(response, req.model),
                    media_type="text/event-stream"
                )
            else:
                # Grok 流式 → 规范化 SSE 格式
                return StreamingResponse(
                    stream_grok_response(response),
                    media_type="text/event-stream"
                )
        else:
            # 非流式响应
            response_data = response.json()
            
            if provider == "gemini":
                # Gemini 非流式 → OpenAI 格式
                return convert_gemini_to_openai(response_data, req.model)
            else:
                # Grok 已经是 OpenAI 格式
                return response_data

    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        
        # 🔒 安全的错误处理
        if status_code == 401:
            error_msg = "API 密钥无效或已过期"
        elif status_code == 429:
            error_msg = "API 速率限制，请稍后重试"
        elif status_code == 500:
            error_msg = "上游服务器错误"
        else:
            error_msg = f"请求失败 (状态码: {status_code})"
        
        logger.error(f"{provider} API 错误: {status_code}")
        raise HTTPException(status_code=status_code, detail=error_msg)
        
    except httpx.TimeoutException:
        logger.error(f"{provider} API 超时")
        raise HTTPException(
            status_code=504,
            detail="请求超时，请稍后重试"
        )
    except Exception as e:
        logger.error(f"未预期错误: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail="服务器内部错误"
        )

@app.get("/v1/models")
@app.get("/{provider}/v1/models")
async def get_models(request: Request, provider: str = None):
    """获取可用模型列表 - OpenAI 兼容格式"""
    
    # 🔒 速率限制
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="请求过于频繁"
        )
    
    if provider:
        provider = provider.lower()
        if provider not in API_BASES:
            raise HTTPException(status_code=400, detail=f"不支持的提供商: {provider}")
        
        # 返回 OpenAI 格式的模型列表
        return {
            "object": "list",
            "data": [
                {
                    "id": model,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": provider,
                    "permission": [],
                    "root": model,
                    "parent": None
                }
                for model in ALLOWED_MODELS[provider]
            ]
        }
    else:
        # 返回所有模型
        all_models = []
        for prov, models in ALLOWED_MODELS.items():
            for model in models:
                all_models.append({
                    "id": model,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": prov,
                    "permission": [],
                    "root": model,
                    "parent": None
                })
        
        return {"object": "list", "data": all_models}

@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {
        "status": "healthy",
        "version": "2.3.0 (OpenAI Compatible)",
        "features": [
            "Gemini → OpenAI 格式转换",
            "标准 SSE 流式输出",
            "速率限制保护",
            "输入验证"
        ],
        "security": {
            "rate_limit": f"{RATE_LIMIT_REQUESTS} req/{RATE_LIMIT_WINDOW}s",
            "access_token": "enabled" if ENABLE_ACCESS_TOKEN else "disabled"
        }
    }

@app.get("/")
async def root():
    """根路径信息"""
    return {
        "service": "Grok & Gemini API Proxy (OpenAI Compatible)",
        "version": "2.3.0",
        "compatibility": "OpenAI API v1",
        "features": [
            "✅ Gemini 自动转换为 OpenAI 格式",
            "✅ 完整支持流式和非流式输出",
            "✅ 标准 SSE (Server-Sent Events) 格式",
            "✅ 速率限制保护",
            "✅ 请求验证和安全防护"
        ],
        "providers": ["grok", "gemini"],
        "models": ALLOWED_MODELS
    }

@app.on_event("shutdown")
async def shutdown_event():
    """关闭时清理资源"""
    await http_client.aclose()
    logger.info("服务关闭")

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info"
    )
