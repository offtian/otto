"""
Evaluation harnesses — LLM evals, quality gates, regression scoring.

Sits below ``application`` and above ``config`` so eval runs can wire real
dependencies via ``get_config()`` without reaching into use-case code.
"""
