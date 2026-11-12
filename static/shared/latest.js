// Only the latest answer counts.
//
// Every call to run() carries a snapshot of the inputs it answers. Starting a new run
// aborts the previous one, and a run that was overtaken or cancelled resolves
// {stale: true} instead of its value or its error. Callers must not touch the page
// (results, status line, buttons) for a stale run, not even in a finally block.

export function createLatest() {
  let seq = 0;
  let controller = null;
  let snapshot;

  return {
    async run(snap, fn) {
      if (controller) controller.abort();
      const mine = ++seq;
      const ctrl = new AbortController();
      controller = ctrl;
      snapshot = snap;
      const stale = () => mine !== seq || ctrl.signal.aborted;
      try {
        const value = await fn(ctrl.signal);
        if (stale()) return { stale: true };
        return { stale: false, value };
      } catch (error) {
        if (stale()) return { stale: true };
        throw error;
      } finally {
        if (controller === ctrl) controller = null;
      }
    },

    // The snapshot of the newest run (undefined after cancel()).
    current() { return snapshot; },

    // True while the newest run has not settled.
    get busy() { return controller !== null; },

    // Invalidate whatever is in flight; it will resolve stale.
    cancel() {
      seq++;
      if (controller) controller.abort();
      controller = null;
      snapshot = undefined;
    },
  };
}
