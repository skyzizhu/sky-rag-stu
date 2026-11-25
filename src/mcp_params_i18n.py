"""Localized labels for the MCP search settings shown in the parameter overview."""

from src import i18n


_MCP_PARAMS = {
    "zh-CN": {
        "params.group.mcp": "MCP 检索参数",
        "params.mcp_top_k_default.label": "默认检索卡片数 top_k",
        "params.mcp_top_k_default.note": "普通检索在调用方省略 top_k 时采用的目标卡片数",
        "params.mcp_top_k_max.label": "单次检索卡片数上限 top_k",
        "params.mcp_top_k_max.note": "调用方在普通检索中可请求的最高目标卡片数，相邻卡片可能合并返回",
        "params.mcp_evidence_tokens_default.label": "默认证据预算（tokens）",
        "params.mcp_evidence_tokens_default.note": "调用方省略 max_evidence_tokens 时，普通检索的估算资料预算",
        "params.mcp_evidence_tokens_max.label": "自动扩容与显式证据预算上限（tokens）",
        "params.mcp_evidence_tokens_max.note": "调用方可通过 max_evidence_tokens 调整语义检索的资料预算，项目比较按项目分配",
        "params.mcp.caption": "适用于 MCP knowledge_search。省略预算时，语义列举和文件清单使用下方的列举与目录预算。",
    },
    "zh-TW": {
        "params.group.mcp": "MCP 檢索參數",
        "params.mcp_top_k_default.label": "預設檢索卡片數 top_k",
        "params.mcp_top_k_default.note": "一般檢索在呼叫端省略 top_k 時採用的目標卡片數",
        "params.mcp_top_k_max.label": "單次檢索卡片數上限 top_k",
        "params.mcp_top_k_max.note": "呼叫端在一般檢索中可要求的最高目標卡片數，相鄰卡片可能合併回傳",
        "params.mcp_evidence_tokens_default.label": "預設證據預算（tokens）",
        "params.mcp_evidence_tokens_default.note": "呼叫端省略 max_evidence_tokens 時，一般檢索的估算資料預算",
        "params.mcp_evidence_tokens_max.label": "自動擴容與明確指定的證據預算上限（tokens）",
        "params.mcp_evidence_tokens_max.note": "呼叫端可透過 max_evidence_tokens 調整語意檢索的資料預算，專案比較按專案分配",
        "params.mcp.caption": "適用於 MCP knowledge_search。省略預算時，語意列舉和檔案清單使用下方的列舉與目錄預算。",
    },
    "en": {
        "params.group.mcp": "MCP search parameters",
        "params.mcp_top_k_default.label": "Default card count (top_k)",
        "params.mcp_top_k_default.note": "Target card count for ordinary searches when the caller omits top_k",
        "params.mcp_top_k_max.label": "Maximum card count per request (top_k)",
        "params.mcp_top_k_max.note": "Highest target card count for ordinary searches. Adjacent cards may be merged in the response.",
        "params.mcp_evidence_tokens_default.label": "Default evidence budget (tokens)",
        "params.mcp_evidence_tokens_default.note": "Estimated evidence budget for ordinary searches when the caller omits max_evidence_tokens",
        "params.mcp_evidence_tokens_max.label": "Maximum automatic or explicit evidence budget (tokens)",
        "params.mcp_evidence_tokens_max.note": "Callers can set max_evidence_tokens for semantic searches. Project comparisons divide the budget across projects.",
        "params.mcp.caption": "Applies to MCP knowledge_search. When no budget is specified, semantic enumeration and file listings use their respective budgets below.",
    },
    "ja": {
        "params.group.mcp": "MCP 検索パラメータ",
        "params.mcp_top_k_default.label": "既定の検索カード数（top_k）",
        "params.mcp_top_k_default.note": "呼び出し元が top_k を省略した場合の通常検索の目標カード数",
        "params.mcp_top_k_max.label": "リクエストごとの検索カード数上限（top_k）",
        "params.mcp_top_k_max.note": "通常検索で要求できる目標カード数の上限。隣接するカードは結合して返す場合があります。",
        "params.mcp_evidence_tokens_default.label": "既定の根拠資料予算（トークン）",
        "params.mcp_evidence_tokens_default.note": "呼び出し元が max_evidence_tokens を省略した場合の通常検索の資料予算（推定）",
        "params.mcp_evidence_tokens_max.label": "自動拡張・明示指定時の資料予算上限（トークン）",
        "params.mcp_evidence_tokens_max.note": "max_evidence_tokens で意味検索の資料予算を指定します。プロジェクト比較ではプロジェクトごとに配分されます。",
        "params.mcp.caption": "MCP の knowledge_search に適用されます。予算を省略すると、意味的な列挙とファイル一覧には下記の各予算が適用されます。",
    },
}


def mcp_param_text(key: str) -> str:
    return _MCP_PARAMS.get(i18n.current_language(), _MCP_PARAMS["zh-CN"])[key]
