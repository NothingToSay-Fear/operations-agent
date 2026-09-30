import pytest

from app import access, knowledge
from app.models import Document, User


async def test_retrieval_filters_owner_and_selection_before_scoring(database, settings):
    async with database.sessions() as session:
        session.add(User(id="bob", username="bob", password_hash="disabled"))
        await session.flush()
        docs = [
            Document(user_id="test-user", title="用户规范", content="独特检索暗号青松，标题使用准确属性。"),
            Document(
                user_id="test-user", title="停用规范", content="独特检索暗号青松，不应检索。", enabled=0
            ),
            Document(user_id="bob", title="其他用户规范", content="独特检索暗号青松，私有。"),
        ]
        for doc in docs:
            session.add(doc)
            await session.flush()
            await knowledge.index_document(session, doc, settings)
        await session.commit()
        async with database.read_sessions() as commerce:
            result = await knowledge.search(session, commerce, "test-user", "独特检索暗号青松", settings)
            assert [r["document_id"] for r in result["rows"] if r["source"] == "uploaded"] == [docs[0].id]
            with pytest.raises(ValueError):
                await knowledge.read(session, commerce, "test-user", docs[2].id)
            with pytest.raises(ValueError):
                await knowledge.read(session, commerce, "test-user", docs[1].id)
            assert (await knowledge.read(session, commerce, "test-user", docs[0].id))["text"] == docs[
                0
            ].content


async def test_commerce_reference_documents_are_not_hidden_knowledge_sources(database, settings):
    async with database.sessions() as session, database.read_sessions() as commerce:
        result = await knowledge.search(session, commerce, "test-user", "秋日回馈 CAM-02", settings)
        assert result["rows"] == []
        with pytest.raises(ValueError):
            await knowledge.read(session, commerce, "test-user", "DOC-CAM-02")
        assert not await access.references_allowed(
            session,
            "test-user",
            {"data": {"rows": [{"source": "simulated_reference", "document_id": "DOC-CAM-02"}]}},
        )


def test_document_parser_rejects_unsupported_and_empty():
    with pytest.raises(ValueError):
        knowledge.parse_document("file.exe", b"x")
    with pytest.raises(ValueError):
        knowledge.parse_document("file.md", b"  ")
    assert knowledge.parse_document("file.md", "品牌规范".encode()) == "品牌规范"
