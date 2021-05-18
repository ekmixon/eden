/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

use anyhow::anyhow;
use blobrepo::BlobRepo;
use blobstore::Loadable;
use bookmarks::{BookmarkName, BookmarkTransactionError, BookmarkUpdateReason};
use commit_transformation::{create_source_to_target_multi_mover, rewrite_commit, upload_commits};
use context::CoreContext;
use futures::{FutureExt, TryFutureExt};
use megarepo_config::{
    MononokeMegarepoConfigs, Source, SourceMappingRules, SourceRevision, SyncTargetConfig, Target,
};
use megarepo_error::MegarepoError;
use megarepo_mapping::MegarepoMapping;
use mononoke_api::Mononoke;
use mononoke_api::RepoContext;
use mononoke_types::{BonsaiChangeset, ChangesetId, RepositoryId};
use reachabilityindex::LeastCommonAncestorsHint;
use std::collections::HashMap;
use std::convert::TryInto;
use std::sync::Arc;

pub(crate) struct SyncChangeset<'a> {
    megarepo_configs: &'a Arc<dyn MononokeMegarepoConfigs>,
    mononoke: &'a Arc<Mononoke>,
}

impl<'a> SyncChangeset<'a> {
    pub(crate) fn new(
        megarepo_configs: &'a Arc<dyn MononokeMegarepoConfigs>,
        mononoke: &'a Arc<Mononoke>,
    ) -> Self {
        Self {
            megarepo_configs,
            mononoke,
        }
    }

    pub(crate) async fn sync(
        &self,
        ctx: &CoreContext,
        source_cs_id: ChangesetId,
        source_name: String,
        target: Target,
        target_megarepo_mapping: Arc<MegarepoMapping>,
    ) -> Result<(), MegarepoError> {
        let target_repo = self.find_repo_by_id(&ctx, target.repo_id).await?;

        // Now we need to find the target config version that was used to create the latest
        // target commit. This config version will be used to sync the new changeset
        let (target_bookmark, target_cs_id) =
            find_target_bookmark_and_value(&ctx, &target_repo, &target).await?;

        let target_config = find_target_sync_config(
            &ctx,
            &target_megarepo_mapping,
            &target,
            target_cs_id,
            &self.megarepo_configs,
        )
        .await?;

        // Given the SyncTargetConfig, let's find config for the source
        // we are going to sync from
        let source_config = find_source_config(&source_name, &target_config)?;

        // Find source repo and changeset that we need to sync
        let source_repo = self.find_repo_by_id(&ctx, source_config.repo_id).await?;
        let source_cs = source_cs_id
            .load(&ctx, source_repo.blob_repo().blobstore())
            .await?;

        // Check if we can sync this commit at all
        if source_cs.is_merge() {
            return Err(MegarepoError::request(anyhow!(
                "{} is a merge commit, and syncing of merge commits is not supported yet",
                source_cs.get_changeset_id()
            )));
        }
        validate_can_sync_changeset(
            &ctx,
            &target,
            &source_cs,
            &target_megarepo_mapping,
            &source_repo,
            &source_config,
        )
        .await?;

        // Finally create a commit in the target and update the mapping.
        let source_cs_id = source_cs.get_changeset_id();
        let new_target_cs_id = sync_changeset_to_target(
            &ctx,
            &source_config.mapping,
            source_repo.blob_repo(),
            source_cs,
            target_repo.blob_repo(),
            target_cs_id,
            &target,
        )
        .await?;

        target_megarepo_mapping
            .insert_source_target_cs_mapping(
                &ctx,
                &source_name,
                &target,
                source_cs_id,
                new_target_cs_id,
                &target_config.version,
            )
            .await?;

        // Move the bookmark and record latest synced source changeset
        let res = update_target_bookmark(
            &ctx,
            target_repo.blob_repo(),
            target_bookmark,
            target_cs_id,
            new_target_cs_id,
            target_megarepo_mapping,
            source_name,
            source_cs_id,
            target,
        )
        .await?;

        if !res {
            // TODO(stash): we might want a special exception type for this case
            return Err(MegarepoError::request(anyhow!(
                "race condition - target bookmark moved while request was executing",
            )));
        }

        Ok(())
    }

    async fn find_repo_by_id(
        &self,
        ctx: &CoreContext,
        repo_id: i64,
    ) -> Result<RepoContext, MegarepoError> {
        let target_repo_id = RepositoryId::new(repo_id.try_into().unwrap());
        let target_repo = self
            .mononoke
            .repo_by_id(ctx.clone(), target_repo_id)
            .await
            .map_err(MegarepoError::internal)?
            .ok_or_else(|| MegarepoError::request(anyhow!("repo not found {}", target_repo_id)))?;
        Ok(target_repo)
    }
}

async fn find_target_bookmark_and_value(
    ctx: &CoreContext,
    target_repo: &RepoContext,
    target: &Target,
) -> Result<(BookmarkName, ChangesetId), MegarepoError> {
    find_bookmark_and_value(ctx, target_repo, &target.bookmark).await
}

async fn find_bookmark_and_value(
    ctx: &CoreContext,
    repo: &RepoContext,
    bookmark_name: &str,
) -> Result<(BookmarkName, ChangesetId), MegarepoError> {
    let bookmark = BookmarkName::new(bookmark_name.to_string()).map_err(MegarepoError::request)?;

    let cs_id = repo
        .blob_repo()
        .bookmarks()
        .get(ctx.clone(), &bookmark)
        .map_err(MegarepoError::internal)
        .await?
        .ok_or_else(|| MegarepoError::request(anyhow!("bookmark {} not found", bookmark)))?;

    Ok((bookmark, cs_id))
}

async fn find_target_sync_config<'a>(
    ctx: &'a CoreContext,
    target_megarepo_mapping: &'a MegarepoMapping,
    target: &'a Target,
    target_cs_id: ChangesetId,
    megarepo_configs: &Arc<dyn MononokeMegarepoConfigs>,
) -> Result<SyncTargetConfig, MegarepoError> {
    let target_config_version = target_megarepo_mapping
        .get_target_config_version(&ctx, &target, target_cs_id)
        .await
        .map_err(MegarepoError::internal)?
        .ok_or_else(|| MegarepoError::request(anyhow!("no target exists {:?}", target)))?;

    // We have a target config version - let's fetch target config itself.
    let target_config = megarepo_configs.get_config_by_version(
        ctx.clone(),
        target.clone(),
        target_config_version,
    )?;

    Ok(target_config)
}

fn find_source_config<'a, 'b>(
    source_name: &'a str,
    target_config: &'b SyncTargetConfig,
) -> Result<&'b Source, MegarepoError> {
    let mut maybe_source_config = None;
    for source in &target_config.sources {
        if source_name == source.source_name {
            maybe_source_config = Some(source);
            break;
        }
    }
    let source_config = maybe_source_config.ok_or_else(|| {
        MegarepoError::request(anyhow!("config for source {} not found", source_name))
    })?;

    Ok(source_config)
}

// We allow syncing changeset from a source if one of its parents was the latest synced changeset
// from this source into this target.
async fn validate_can_sync_changeset(
    ctx: &CoreContext,
    target: &Target,
    source_cs: &BonsaiChangeset,
    target_megarepo_mapping: &MegarepoMapping,
    source_repo: &RepoContext,
    source: &Source,
) -> Result<(), MegarepoError> {
    match &source.revision {
        SourceRevision::hash(_) => {
            return Err(MegarepoError::request(anyhow!(
                "can't sync changeset from source {} because this source points to a changeset",
                source.source_name,
            )));
        }
        SourceRevision::bookmark(bookmark) => {
            let (_, source_bookmark_value) =
                find_bookmark_and_value(ctx, source_repo, &bookmark).await?;

            if source_bookmark_value != source_cs.get_changeset_id() {
                let is_ancestor = source_repo
                    .skiplist_index()
                    .is_ancestor(
                        ctx,
                        &source_repo.blob_repo().get_changeset_fetcher(),
                        source_cs.get_changeset_id(),
                        source_bookmark_value,
                    )
                    .await
                    .map_err(MegarepoError::internal)?;

                if is_ancestor {
                    return Err(MegarepoError::request(anyhow!(
                        "{} is not an ancestor of source bookmark {}",
                        source_bookmark_value,
                        bookmark,
                    )));
                }
            }
        }
        SourceRevision::UnknownField(_) => {
            return Err(MegarepoError::internal(anyhow!(
                "unexpected source revision!"
            )));
        }
    };

    let maybe_latest_synced_cs_id = target_megarepo_mapping
        .get_latest_synced_commit_from_source(&ctx, &source.source_name, &target)
        .await
        .map_err(MegarepoError::internal)?;

    match maybe_latest_synced_cs_id {
        Some(latest_synced_cs_id) => {
            let found = source_cs.parents().find(|p| p == &latest_synced_cs_id);
            if found.is_none() {
                return Err(MegarepoError::request(anyhow!(
                    "Can't sync {}, because its parent are not synced yet to the target. \
                            Latest synced source changeset is {}",
                    source_cs.get_changeset_id(),
                    latest_synced_cs_id,
                )));
            }
        }
        None => {
            return Err(MegarepoError::internal(anyhow!(
                "Target {:?} was not synced into unified branch yet",
                target
            )));
        }
    };

    Ok(())
}

async fn sync_changeset_to_target(
    ctx: &CoreContext,
    mapping: &SourceMappingRules,
    source_repo: &BlobRepo,
    source_cs: BonsaiChangeset,
    target_repo: &BlobRepo,
    target_cs_id: ChangesetId,
    target: &Target,
) -> Result<ChangesetId, MegarepoError> {
    let mover =
        create_source_to_target_multi_mover(mapping.clone()).map_err(MegarepoError::internal)?;

    let source_cs_id = source_cs.get_changeset_id();
    // Create a new commit using a mover
    let source_cs_mut = source_cs.into_mut();
    let mut remapped_parents = HashMap::new();
    match (source_cs_mut.parents.get(0), source_cs_mut.parents.get(1)) {
        (Some(parent), None) => {
            remapped_parents.insert(*parent, target_cs_id);
        }
        _ => {
            return Err(MegarepoError::request(anyhow!(
                "expected exactly one parent, found {}",
                source_cs_mut.parents.len()
            )));
        }
    }

    let rewritten_commit = rewrite_commit(
        &ctx,
        source_cs_mut,
        &remapped_parents,
        mover,
        source_repo.clone(),
    )
    .await
    .map_err(MegarepoError::internal)?
    .ok_or_else(|| {
        MegarepoError::internal(anyhow!(
            "failed to rewrite commit {}, target: {:?}",
            source_cs_id,
            target
        ))
    })?;

    let rewritten_commit = rewritten_commit.freeze().map_err(MegarepoError::internal)?;
    let target_cs_id = rewritten_commit.get_changeset_id();
    upload_commits(&ctx, vec![rewritten_commit], source_repo, target_repo)
        .await
        .map_err(MegarepoError::internal)?;

    Ok(target_cs_id)
}

async fn update_target_bookmark(
    ctx: &CoreContext,
    target_repo: &BlobRepo,
    bookmark: BookmarkName,
    from_target_cs_id: ChangesetId,
    to_target_cs_id: ChangesetId,
    target_megarepo_mapping: Arc<MegarepoMapping>,
    source_name: String,
    source_cs_id: ChangesetId,
    target: Target,
) -> Result<bool, MegarepoError> {
    let mut bookmark_txn = target_repo.bookmarks().create_transaction(ctx.clone());

    bookmark_txn
        .update(
            &bookmark,
            to_target_cs_id,
            from_target_cs_id,
            BookmarkUpdateReason::XRepoSync,
            None,
        )
        .map_err(MegarepoError::internal)?;

    let res = bookmark_txn
        .commit_with_hook(Arc::new(move |ctx, txn| {
            let source_name = source_name.clone();
            let target = target.clone();
            let target_megarepo_mapping = target_megarepo_mapping.clone();
            async move {
                target_megarepo_mapping
                    .update_latest_synced_commit_from_source(
                        &ctx,
                        txn,
                        &source_name,
                        &target,
                        source_cs_id,
                    )
                    .await
                    .map_err(BookmarkTransactionError::Other)
            }
            .boxed()
        }))
        .await
        .map_err(MegarepoError::internal)?;

    Ok(res)
}