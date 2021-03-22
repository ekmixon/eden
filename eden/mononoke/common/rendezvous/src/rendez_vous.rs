/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

use anyhow::Error;
use fbinit::FacebookInit;
use futures::future::{BoxFuture, Future, FutureExt, Shared};
use futures_stats::TimedFutureExt;
use shared_error::anyhow::{IntoSharedError, SharedError};
use std::collections::{HashMap, HashSet};
use std::hash::Hash;
use std::sync::{Arc, Mutex};
use time_ext::DurationExt;
use tokio::sync::Notify;

use crate::{RendezVousStats, TunablesRendezVousController};

/// The RendezVousController controls the behavior of a RendezVous instance. It notably decides
/// when to wait for a batch to build up and when to kick off queries.
#[async_trait::async_trait]
pub trait RendezVousController: Send + Sync + 'static {
    /// Decide whether we should be starting a new batch. This will be called once per request that
    /// arrives to the RendezVous instance. It is expected that the RendezVousController might
    /// store some internal state to make this decision.
    fn should_batch(&self) -> bool;

    /// Delay sending a batch to give ourselves a chance to accumulate some data. The batch will be
    /// kicked off once this future returns;
    async fn wait_for_dispatch(&self);

    /// If our number of queued keys exceeds this threshold, then we'll dispatch the query even if
    /// wait_for_dispatch hasn't returned yet.
    fn early_dispatch_threshold(&self) -> usize;
}

struct RendezVousInner<K, V, C> {
    staging: Mutex<
        Option<(
            HashSet<K>,
            Shared<BoxFuture<'static, Result<Arc<HashMap<K, V>>, SharedError>>>,
            Arc<Notify>,
        )>,
    >,
    controller: C,
    stats: Arc<RendezVousStats>,
}

pub struct RendezVous<K, V, C = TunablesRendezVousController> {
    inner: Arc<RendezVousInner<K, V, C>>,
}

impl<K, V, C> Clone for RendezVous<K, V, C> {
    fn clone(&self) -> Self {
        Self {
            inner: self.inner.clone(),
        }
    }
}

impl<K, V, C> RendezVous<K, V, C> {
    pub fn new(controller: C, stats: Arc<RendezVousStats>) -> Self {
        Self {
            inner: Arc::new(RendezVousInner {
                staging: Mutex::new(None),
                controller,
                stats,
            }),
        }
    }
}

impl<K, V, C> RendezVous<K, V, C>
where
    K: Clone + Eq + Hash + Send + Sync + 'static,
    V: Clone + Send + Sync + 'static,
    C: RendezVousController,
{
    pub fn dispatch<F0, F1, Fut>(
        &self,
        fb: FacebookInit,
        keys: HashSet<K>,
        f0: F0,
    ) -> impl Future<Output = Result<HashMap<K, Option<V>>, Error>>
    where
        F0: FnOnce() -> F1, // Can construct a F1 if we are the first caller here
        F1: FnOnce(HashSet<K>) -> Fut + Send + 'static, // Actually makes the call
        Fut: Future<Output = Result<HashMap<K, V>, Error>> + Send,
    {
        if self.inner.controller.should_batch()
            && keys.len() < self.inner.controller.early_dispatch_threshold()
        {
            self.dispatch_batched(fb, keys, f0).left_future()
        } else {
            self.dispatch_not_batched(fb, keys, f0).right_future()
        }
    }

    fn dispatch_batched<F0, F1, Fut>(
        &self,
        fb: FacebookInit,
        keys: HashSet<K>,
        f0: F0,
    ) -> impl Future<Output = Result<HashMap<K, Option<V>>, Error>>
    where
        F0: FnOnce() -> F1,
        F1: FnOnce(HashSet<K>) -> Fut + Send + 'static,
        Fut: Future<Output = Result<HashMap<K, V>, Error>> + Send,
    {
        let mut deduplicated = 0;

        let mut guard = self.inner.staging.lock().expect("Poisoned lock");

        let fut = match &mut *guard {
            guard @ None => {
                let inner = self.inner.clone();
                let f1 = f0();

                let notify = Arc::new(Notify::new());

                let fut = {
                    let notify = notify.clone();

                    async move {
                        let is_early = futures::select! {
                            _ = inner.controller.wait_for_dispatch().fuse() => false,
                            _ = notify.notified().fuse() => true,
                        };

                        if is_early {
                            inner.stats.dispatch_batch_early.add_value(1);
                        } else {
                            inner.stats.dispatch_batch_scheduled.add_value(1);
                        }

                        let (keys, _, _) = inner
                            .staging
                            .lock()
                            .expect("Poisoned lock")
                            .take()
                            .expect("Staging cannot be empty if a task was dispatched");

                        let ret = dispatch_with_stats(fb, f1, keys, &inner.stats)
                            .await
                            .shared_error()?;

                        Result::<_, SharedError>::Ok(Arc::new(ret))
                    }
                }
                .boxed()
                .shared();

                *guard = Some((keys.clone(), fut.clone(), notify));

                fut
            }
            Some((ref mut staged_keys, ref fut, ref notify)) => {
                for k in keys.iter().cloned() {
                    if !staged_keys.insert(k) {
                        deduplicated += 1;
                    }
                }

                if staged_keys.len() >= self.inner.controller.early_dispatch_threshold() {
                    notify.notify();
                }

                fut.clone()
            }
        };

        std::mem::drop(guard);

        self.inner.stats.keys_deduplicated.add_value(deduplicated);

        async move {
            let shared_ret = fut.await?;
            let ret = keys
                .into_iter()
                .map(|k| {
                    let v = shared_ret.get(&k).cloned();
                    (k, v)
                })
                .collect();
            Ok(ret)
        }
    }

    fn dispatch_not_batched<F0, F1, Fut>(
        &self,
        fb: FacebookInit,
        keys: HashSet<K>,
        f0: F0,
    ) -> impl Future<Output = Result<HashMap<K, Option<V>>, Error>>
    where
        F0: FnOnce() -> F1,
        F1: FnOnce(HashSet<K>) -> Fut + Send + 'static,
        Fut: Future<Output = Result<HashMap<K, V>, Error>> + Send,
    {
        let inner = self.inner.clone();

        async move {
            inner.stats.dispatch_no_batch.add_value(1);

            let mut ret = dispatch_with_stats(fb, f0(), keys.clone(), &inner.stats).await?;

            let ret = keys
                .into_iter()
                .map(|k| {
                    let v = ret.remove(&k);
                    (k, v)
                })
                .collect();

            Ok(ret)
        }
    }
}

async fn dispatch_with_stats<K, V, F1, Fut>(
    fb: FacebookInit,
    f1: F1,
    keys: HashSet<K>,
    rdv_stats: &RendezVousStats,
) -> Result<HashMap<K, V>, Error>
where
    F1: FnOnce(HashSet<K>) -> Fut + Send + 'static,
    Fut: Future<Output = Result<HashMap<K, V>, Error>>,
{
    rdv_stats.keys_dispatched.add_value(keys.len() as i64);

    rdv_stats.inflight.increment_value(fb, 1);
    let (stats, ret) = f1(keys).timed().await;
    rdv_stats.inflight.increment_value(fb, -1);

    rdv_stats
        .fetch_completion_time_ms
        .add_value(stats.completion_time.as_millis_unchecked() as i64);

    ret
}