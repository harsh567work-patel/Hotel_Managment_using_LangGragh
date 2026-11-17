# SpiceRoute Kitchen — Food Ordering RAG System

An AI-powered food ordering chatbot built with **LangGraph**, **Groq** (LLM), and **Qdrant** (vector DB).

---

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Set up environment variables
```bash
copy .env.example .env
# Then edit .env and add your keys
```

### 3. Load the knowledge base into Qdrant
```bash
python -m rag.ingest
```

### 4. Launch the chatbot
```bash
streamlit run app.py
```

### 5. (Optional) Run RAG evaluation
```bash
python -m eval.evaluator
```

---

## Project Structure

```
d:\langGRAPH\
├── app.py                      ← Streamlit frontend
├── graph/
│   ├── state.py                ← OrderState schema
│   ├── graph_builder.py        ← LangGraph graph + routing
│   └── nodes/                  ← Individual processing nodes
├── rag/
│   ├── qdrant_client.py        ← Qdrant connection + search
│   └── ingest.py               ← Load menu data into Qdrant
├── data/
│   ├── menu.json               ← Restaurant knowledge base
│   └── golden_dataset.json     ← QA eval pairs
├── eval/
│   └── evaluator.py            ← RAG accuracy scorer
├── .env.example                ← Environment variable template
└── requirements.txt
```

---

## Environment Variables

| Variable | Description |
|---|---|
| `GROQ_API_KEY` | From console.groq.com |
| `QDRANT_API_KEY` | From cloud.qdrant.io |
| `QDRANT_URL` | Your Qdrant cluster URL |

> If Qdrant keys are not set, the system falls back to in-memory Qdrant (dev mode).
