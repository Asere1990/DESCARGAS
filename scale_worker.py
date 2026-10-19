import asyncio
import os
import signal
import time
from descargas_app.scalable import ScaleService
async def main():
    os.umask(0o077);os.environ['DESCARGAS_ROLE']='worker'
    service=ScaleService();stop=asyncio.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):asyncio.get_running_loop().add_signal_handler(sig,stop.set)
    await service.start()
    try:
        deadline=time.monotonic()+120
        await asyncio.sleep(1)
        while not service.ready:
            if time.monotonic()>deadline or service.state=='startup_error' or not service.cipher or not service.repo.url:
                raise RuntimeError('Worker configuration/startup failed')
            await asyncio.sleep(1)
        while not stop.is_set():
            if any(task.done() for task in service.tasks[1:]):
                raise RuntimeError('Worker task stopped unexpectedly')
            try:await asyncio.wait_for(stop.wait(),timeout=2)
            except TimeoutError:pass
    finally:await service.close()
if __name__=='__main__':asyncio.run(main())
