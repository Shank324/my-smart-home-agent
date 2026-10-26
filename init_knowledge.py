from pathlib import Path

from app.services.agent.knowledge.retriever import KnowledgeRetriever


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    count = KnowledgeRetriever(data_dir=root / "seed_data",
                               store_path=root / ".data" / "knowledge.json").rebuild()
    print(f"indexed {count} knowledge chunks")
