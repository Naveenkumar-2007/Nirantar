import asyncio
import contextlib

from nirantar.core.dotenv import load_dotenv

load_dotenv()            # before importing the runner: its modules read configuration from the environment

from nirantar.services.runner import main  # noqa: E402

with contextlib.suppress(KeyboardInterrupt):
    asyncio.run(main())
