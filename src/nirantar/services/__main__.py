import asyncio
import contextlib

from nirantar.services.runner import main

with contextlib.suppress(KeyboardInterrupt):
    asyncio.run(main())
