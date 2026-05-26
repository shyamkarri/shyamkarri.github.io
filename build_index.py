import os
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_text_splitters import RecursiveCharacterTextSplitter

def build_vector_db():
    print("Building FAISS index...")
    try:
        with open("knowledge.txt", "r") as f:
            knowledge_text = f.read()
    except FileNotFoundError:
        knowledge_text = "Karri Prasad is an AI Engineer."

    text_splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
    docs = text_splitter.create_documents([knowledge_text])

    # Cache model locally during build phase
    embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2", cache_folder="./.cache")
    
    vectorstore = FAISS.from_documents(docs, embeddings)
    
    # Save the index to a local directory
    vectorstore.save_local("faiss_index")
    print("FAISS index saved successfully to 'faiss_index' directory.")

if __name__ == "__main__":
    build_vector_db()
