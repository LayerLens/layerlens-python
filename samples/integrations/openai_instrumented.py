"""Example: Instrument OpenAI and record each call in a trace.

Requires:
    uv pip install --extra-index-url https://sdk.layerlens.ai/package "layerlens[openai]"
    export LAYERLENS_API_KEY="your-api-key"
    export OPENAI_API_KEY="your-openai-key"
"""

import openai
from layerlens import LayerLens
from layerlens.instrument import emit, span, trace
from layerlens.instrument.adapters.providers.openai import instrument_openai


def main() -> None:
    client = LayerLens()
    openai_client = openai.OpenAI()

    # Instrument the OpenAI client — every chat.completions.create call made
    # inside a @trace records model.invoke and cost.record automatically.
    instrument_openai(openai_client)

    @trace(client)
    def qa_agent(question: str):
        """Simple Q&A agent with a retrieval step and an LLM call."""

        # A span of its own for the retrieval step, and the event it records
        with span("retrieve"):
            # In a real app, this would query a vector database
            docs = [
                "Python is a programming language.",
                "It was created by Guido van Rossum.",
            ]
            emit("retrieval.query", {"query": question, "result_count": len(docs)})

        # The OpenAI call is recorded by the adapter — no span() or emit() needed
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": f"Answer using this context: {docs}"},
                {"role": "user", "content": question},
            ],
        )

        return response.choices[0].message.content

    answer = qa_agent("What is Python and who created it?")
    print(f"Answer: {answer}")


if __name__ == "__main__":
    main()
