import tempfile
def test_chroma_upsert():
    import chromadb
    from chromadb.config import Settings
    d = tempfile.mkdtemp(prefix="chromamin-")
    client = chromadb.PersistentClient(path=d, settings=Settings(anonymized_telemetry=False))
    col = client.get_or_create_collection(name="memories", metadata={"hnsw:space": "cosine"})
    col.upsert(ids=["m1"], embeddings=[[0.1]*64], documents=["甲"], metadatas=[{"chash":"x","category":"fact"}])
    assert col.count() == 1
