"""保留检索文档边界，按完整原文去重；不改写文档内部的行与空白。"""

from collections.abc import Iterable
import re


_HEADER = re.compile(r"<!-- rag-document characters=([0-9]+) -->\n")
_FOOTER = "\n<!-- /rag-document -->"
_SEPARATOR = "\n\n"


# 【修复】按字符长度定位整篇原文，正文中的标题或注释不会被误当成边界。
def join_context_documents(documents: Iterable[str]) -> str:
    return _SEPARATOR.join(
        f"<!-- rag-document characters={len(document)} -->\n{document}{_FOOTER}"
        for document in documents if document.strip()
    )


def split_context_documents(context: str) -> list[str]:
    """新格式全部解析成功才拆分；旧资料或损坏的格式整段保留。"""
    if not context.strip():
        return []
    documents: list[str] = []
    offset = 0
    while True:
        header = _HEADER.match(context, offset)
        if header is None:
            return [context]
        size = header.group(1)
        # 过长的数字不可能对应此字符串中的文档，也避免异常的整数解析。
        if len(size) > len(str(len(context))):
            return [context]
        end = header.end() + int(size)
        if not context.startswith(_FOOTER, end):
            return [context]
        documents.append(context[header.end():end])
        offset = end + len(_FOOTER)
        if offset == len(context):
            return documents
        if not context.startswith(_SEPARATOR, offset):
            return [context]
        offset += len(_SEPARATOR)


def merge_contexts(*contexts: str) -> str:
    """首批资料在前；仅全文完全相同的文档保留一份。"""
    documents: list[str] = []
    seen: set[str] = set()
    for context in contexts:
        for document in split_context_documents(context):
            if document not in seen:
                seen.add(document)
                documents.append(document)
    return join_context_documents(documents)
