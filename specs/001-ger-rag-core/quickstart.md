# Quickstart: GER-RAG Core Retrieval System

## Prerequisites

- Python 3.11+
- CUDA-capable GPU (for RURI-v3 embedding)
- ~2GB disk for model download on first run

## Setup

```bash
# Clone and enter project
cd ger-rag

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Run the Server

```bash
uvicorn ger_rag.server.app:app --host 0.0.0.0 --port 8000
```

The server loads the embedding model and initializes the FAISS index on startup.

## Basic Usage

### 1. Index Documents

```bash
curl -X POST http://localhost:8000/index \
  -H "Content-Type: application/json" \
  -d '{
    "documents": [
      {"content": "Pythonは汎用プログラミング言語です。"},
      {"content": "機械学習はAIの一分野です。"},
      {"content": "FastAPIはPythonのWebフレームワークです。"}
    ]
  }'
```

### 2. Query

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"text": "プログラミング言語について", "top_k": 5}'
```

### 3. Inspect Node State

```bash
curl http://localhost:8000/node/{node_id}
```

### 4. Inspect Co-occurrence Graph

```bash
curl http://localhost:8000/graph
```

### 5. Reset Dynamic State

```bash
curl -X POST http://localhost:8000/reset
```

## Configuration

Hyperparameters are configured via `ger_rag/config.py`. Key parameters:

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| top_k     | 10      | Number of results returned per query |
| alpha     | 0.05    | Mass boost scaling |
| delta     | 0.01    | Temporal decay rate |
| rho       | 0.1     | Graph propagation weight |

## Development

```bash
# Run tests
pytest

# Run with auto-reload
uvicorn ger_rag.server.app:app --reload
```
