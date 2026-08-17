"""Golden evals for the incident summary (RC1-267).

Two layers, following RC1-258's split:

* **Layer 1** is `pytest` — the prompt/parser contract, checked on every push
  with no credentials and no token cost. Lives in `tests/unit/test_prompt_contract.py`.
* **Layer 2** is `python -m evals` — binds each fixture incident into the
  shipped prompt, calls the model the template pins, and scores the output.
  Records land in the shared store and render on the public trend page.

The modules importable without `agent-evals` installed (`fixtures`,
`summarizer`) are the ones layer 1 uses; `subject` and `__main__` need
`requirements-evals.txt`.
"""
