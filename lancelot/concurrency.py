"""M2-P1 — bounded structured fan-out. `lancelot.gather(*coros,
concurrency=N)` replaces the 8-15 LOC `Semaphore + asyncio.gather(*,
return_exceptions=True)` pattern that recurs across real AI products
(AEMON_PRODUCT_CODE:911,1066; Tavily 1411,3148; Artificial Societies
1607,1744-1756; MiroFish 413,808-812; Perplexity Replica 603,1875).

Defaults: return_exceptions=True (partial failure is normal in real
LLM/scrape workloads; a single 429 should not cancel the batch).
concurrency=None disables the gate (vanilla asyncio.gather shape).
NO rps cap (strict synthesis — only 1 product cites it)."""
import asyncio


async def gather(*coros, concurrency: int | None = None,
                 return_exceptions: bool = True):
    """Bounded asyncio.gather. Returns results in input order; raised
    exceptions become items in the result list (unless return_exceptions
    is set False explicitly)."""
    if concurrency is None:
        return await asyncio.gather(*coros, return_exceptions=return_exceptions)
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(coro):
        async with sem:
            return await coro

    return await asyncio.gather(*(_bounded(c) for c in coros),
                                return_exceptions=return_exceptions)
