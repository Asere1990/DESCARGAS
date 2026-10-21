"""Dedicated administrative ALMACEN worker; does not consume user download lanes."""
import asyncio
from scale_worker import main

if __name__ == "__main__":
    asyncio.run(main("export"))
