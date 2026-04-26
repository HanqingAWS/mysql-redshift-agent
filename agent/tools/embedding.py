"""Bedrock embedding 客户端（Cohere multilingual v3，1024 维）。

AWS_REGION 环境变量决定就近端点；global 前缀的跨区路由只对 LLM 有效，
embedding 模型按当前 region 调用。
"""
from __future__ import annotations

import json
import os
from functools import lru_cache

import boto3

MODEL_ID = os.environ.get("EMBEDDING_MODEL_ID", "cohere.embed-multilingual-v3")
REGION = os.environ.get("AWS_REGION", "ap-northeast-1")


@lru_cache(maxsize=1)
def _client():
    return boto3.client("bedrock-runtime", region_name=REGION)


def embed(text: str, input_type: str = "search_document") -> list[float]:
    """把一段文本编码为 1024 维向量。

    input_type:
      search_document — 入库时用（要被检索的内容）
      search_query    — 查询时用（发起检索的内容）
    两者 embedding 空间一致但 Cohere 建议区分以提升召回质量。
    """
    body = {"texts": [text], "input_type": input_type}
    resp = _client().invoke_model(modelId=MODEL_ID, body=json.dumps(body))
    data = json.loads(resp["body"].read())
    return data["embeddings"][0]
