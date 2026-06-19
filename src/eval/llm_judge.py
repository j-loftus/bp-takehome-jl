"""
LLM-as-judge for scalable evaluation coverage.

Scores (question, retrieved_context, answer) triples on:
  - Faithfulness: is the answer grounded in the context?
  - Completeness: does it address all parts of the question?
  - Relevance: is the answer on-topic?
"""


def judge(question: str, context: str, answer: str) -> dict:
    """
    Score a single QA triple using an LLM judge.

    Returns:
        {
            "faithfulness": float,    # 0-1
            "completeness": float,    # 0-1
            "relevance": float,       # 0-1
            "reasoning": str,
        }
    """
    # TODO: load judge prompt, call Anthropic API, parse structured scores
    raise NotImplementedError


def run_judge_eval(qa_triples: list[dict]) -> list[dict]:
    """Run judge over a list of {question, context, answer} dicts."""
    # TODO: call judge() for each triple, return results list
    raise NotImplementedError
