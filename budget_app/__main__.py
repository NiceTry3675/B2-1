"""``python -m budget_app <command> [options]``으로 실행할 때의 진입점."""

from .cli import main

raise SystemExit(main())
