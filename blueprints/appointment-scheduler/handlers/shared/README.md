# handlers/shared/

Code here is packaged into every handler Lambda zip. Handlers can import from it as:

```python
from shared.my_module import my_function
```

Use this for code that runs inside handler Lambdas (event parsing, CRM adapters, etc.).
Do NOT put agent tools or skills here — those belong in `src/` and run inside the agent container.

