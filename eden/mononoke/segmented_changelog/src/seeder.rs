/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

use std::sync::Arc;

use anyhow::{anyhow, Context, Result};
use futures::stream::TryStreamExt;
use slog::info;
use std::collections::{HashSet, VecDeque};

use sql_ext::replication::ReplicaLagMonitor;
use stats::prelude::*;

use blobstore::Blobstore;
use bulkops::{Direction, PublicChangesetBulkFetch};
use changesets::{ChangesetEntry, Changesets};
use context::CoreContext;
use mononoke_types::{ChangesetId, RepositoryId};
use phases::Phases;

use crate::iddag::IdDagSaveStore;
use crate::idmap::IdMapFactory;
use crate::idmap::SqlIdMapVersionStore;
use crate::types::{IdMapVersion, SegmentedChangelogVersion};
use crate::update::{self, StartState};
use crate::version_store::SegmentedChangelogVersionStore;
use crate::{Group, InProcessIdDag, SegmentedChangelogSqlConnections};

define_stats! {
    prefix = "mononoke.segmented_changelog.seeder";
    build_all_graph: timeseries(Sum),
}

enum ChangesetBulkFetch {
    Fetch(Arc<PublicChangesetBulkFetch>),
    UsePrefetched {
        prefetched: Vec<ChangesetEntry>,
        changesets: Arc<dyn Changesets>,
    },
}

impl ChangesetBulkFetch {
    async fn fetch(&self, ctx: &CoreContext, head: ChangesetId) -> Result<HashSet<ChangesetEntry>> {
        use ChangesetBulkFetch::*;

        match self {
            Fetch(bulk_fetch) => {
                let cs_entries: HashSet<_> = bulk_fetch
                    // Order doesn't matter here
                    .fetch(ctx, Direction::OldestFirst)
                    .inspect_ok({
                        let mut count = 1;
                        move |_| {
                            count += 1;
                            if count % 100000 == 0 {
                                info!(ctx.logger(), "{} changesets loaded ", count);
                            }
                        }
                    })
                    .try_collect()
                    .await?;
                Ok(cs_entries)
            }
            UsePrefetched {
                prefetched,
                changesets,
            } => {
                let mut visited = HashSet::new();
                for cs in prefetched {
                    visited.insert(cs.cs_id);
                }
                // Check that prefetched changesets are valid i.e. that every parent changeset is present
                for cs in prefetched {
                    for parent in &cs.parents {
                        if !visited.contains(&parent) {
                            return Err(anyhow!(
                                "invalid prefetched changesets - parent {} of {} is not present",
                                parent,
                                cs.cs_id
                            ));
                        }
                    }
                }

                let mut q = VecDeque::new();
                if visited.insert(head) {
                    q.push_back(head);
                }

                let mut res: HashSet<_> = prefetched.clone().into_iter().collect();
                while let Some(cs_id) = q.pop_front() {
                    let cs_entry = changesets
                        .get(ctx.clone(), cs_id)
                        .await?
                        .ok_or_else(|| anyhow!("{} not found", cs_id))?;
                    for parent in &cs_entry.parents {
                        if visited.insert(*parent) {
                            q.push_back(*parent);
                        }
                    }
                    res.insert(cs_entry);
                }

                Ok(res)
            }
        }
    }
}

pub struct SegmentedChangelogSeeder {
    idmap_version_store: SqlIdMapVersionStore,
    changeset_bulk_fetch: ChangesetBulkFetch,
    sc_version_store: SegmentedChangelogVersionStore,
    iddag_save_store: IdDagSaveStore,
    idmap_factory: IdMapFactory,
}

impl SegmentedChangelogSeeder {
    pub fn new(
        repo_id: RepositoryId,
        connections: SegmentedChangelogSqlConnections,
        replica_lag_monitor: Arc<dyn ReplicaLagMonitor>,
        changesets: Arc<dyn Changesets>,
        phases: Arc<dyn Phases>,
        blobstore: Arc<dyn Blobstore>,
        prefetched: Option<Vec<ChangesetEntry>>,
    ) -> Self {
        let idmap_version_store = SqlIdMapVersionStore::new(connections.0.clone(), repo_id);
        let sc_version_store = SegmentedChangelogVersionStore::new(connections.0.clone(), repo_id);
        let iddag_save_store = IdDagSaveStore::new(repo_id, blobstore);
        let changeset_bulk_fetch = match prefetched {
            Some(prefetched) => ChangesetBulkFetch::UsePrefetched {
                prefetched,
                changesets,
            },
            None => ChangesetBulkFetch::Fetch(Arc::new(PublicChangesetBulkFetch::new(
                changesets, phases,
            ))),
        };
        let idmap_factory = IdMapFactory::new(connections.0, replica_lag_monitor, repo_id);
        Self {
            idmap_version_store,
            changeset_bulk_fetch,
            sc_version_store,
            iddag_save_store,
            idmap_factory,
        }
    }

    pub fn new_from_built_dependencies(
        idmap_version_store: SqlIdMapVersionStore,
        changeset_bulk_fetch: Arc<PublicChangesetBulkFetch>,
        sc_version_store: SegmentedChangelogVersionStore,
        iddag_save_store: IdDagSaveStore,
        idmap_factory: IdMapFactory,
    ) -> Self {
        Self {
            idmap_version_store,
            changeset_bulk_fetch: ChangesetBulkFetch::Fetch(changeset_bulk_fetch),
            sc_version_store,
            iddag_save_store,
            idmap_factory,
        }
    }

    pub async fn run(&self, ctx: &CoreContext, head: ChangesetId) -> Result<()> {
        let idmap_version = {
            let v = match self
                .idmap_version_store
                .get(&ctx)
                .await
                .context("error fetching idmap version from store")?
            {
                Some(v) => v.0 + 1,
                None => 1,
            };
            IdMapVersion(v)
        };
        self.run_with_idmap_version(ctx, head, idmap_version).await
    }

    pub async fn run_with_idmap_version(
        &self,
        ctx: &CoreContext,
        head: ChangesetId,
        idmap_version: IdMapVersion,
    ) -> Result<()> {
        STATS::build_all_graph.add_value(1);
        info!(
            ctx.logger(),
            "seeding segmented changelog using idmap version: {}", idmap_version
        );

        let changeset_entries = self.changeset_bulk_fetch.fetch(ctx, head).await?;
        info!(
            ctx.logger(),
            "{} changesets loaded",
            changeset_entries.len()
        );
        let mut start_state = StartState::new();
        for cs_entry in changeset_entries.into_iter() {
            start_state.insert_parents(cs_entry.cs_id, cs_entry.parents);
        }

        let low_dag_id = Group::MASTER.min_id();
        let idmap = self.idmap_factory.for_writer(ctx, idmap_version);
        let mut iddag = InProcessIdDag::new_in_process();

        // Assign ids for all changesets thus creating an IdMap
        let (mem_idmap, head_dag_id) = update::assign_ids(ctx, &start_state, head, low_dag_id)?;
        info!(ctx.logger(), "dag ids assigned");

        // Construct the iddag
        update::update_iddag(ctx, &mut iddag, &start_state, &mem_idmap, head_dag_id)?;
        info!(ctx.logger(), "iddag constructed");

        // Update IdMapVersion
        self.idmap_version_store
            .set(&ctx, idmap_version)
            .await
            .context("updating idmap version")?;
        info!(ctx.logger(), "idmap version bumped");

        // Write IdMap (to SQL table)
        update::update_idmap(ctx, &idmap, &mem_idmap).await?;
        info!(ctx.logger(), "idmap written");

        // Write the IdDag (to BlobStore)
        let iddag_version = self
            .iddag_save_store
            .save(&ctx, &iddag)
            .await
            .context("error saving iddag")?;

        // Update SegmentedChangelogVersion
        let sc_version = SegmentedChangelogVersion::new(iddag_version, idmap_version);
        self.sc_version_store
            .set(&ctx, sc_version)
            .await
            .context("error updating segmented changelog version store")?;
        info!(
            ctx.logger(),
            "successfully finished seeding segmented changelog",
        );
        Ok(())
    }
}
