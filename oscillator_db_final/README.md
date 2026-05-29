# Mechanical Oscillator Database — Free Local AI Version

This version removes paid API usage. It uses a physics-aware rule extractor plus optional free local AI through Ollama.

No OpenAI API key is required.

## 1. Install Python packages

```bash
pip install -r requirements.txt
```

## 2. Install Ollama for local AI extraction

Install Ollama from https://ollama.com.

Then run:

```bash
ollama pull qwen2.5:7b-instruct
```

Other usable models:

```bash
ollama pull llama3.1:8b
ollama pull mistral
```

## 3. Run the website

```bash
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

## Notes

Best free strategy used here:

```text
local Ollama model + physics regex + strict validation + editable records
```

This avoids paid APIs, but local models are usually weaker than paid frontier models. Always review/edit values after extraction.

If the paper says “room temperature” but gives no exact number, the app stores `temperature_k = 300` and adds a note.
