/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

use std::collections::HashMap;
use std::sync::Arc;

use anyhow::{Error, Result};
use async_trait::async_trait;
use blobrepo::BlobRepo;
use blobstore::{Blobstore, Loadable};
use context::CoreContext;
use derived_data::{
    impl_bonsai_derived_mapping, BlobstoreRootIdMapping, BonsaiDerivable,
    BonsaiDerivedMappingContainer, DerivedDataTypesConfig,
};
use futures::stream::{self, StreamExt, TryStreamExt};
use mononoke_types::{BonsaiChangeset, ChangesetId};

use crate::ChangesetInfo;

#[async_trait]
impl BonsaiDerivable for ChangesetInfo {
    const NAME: &'static str = "changeset_info";

    type Options = ();

    async fn derive_from_parents_impl(
        _ctx: CoreContext,
        _repo: BlobRepo,
        bonsai: BonsaiChangeset,
        _parents: Vec<Self>,
        _options: &Self::Options,
    ) -> Result<Self, Error> {
        let csid = bonsai.get_changeset_id();
        Ok(ChangesetInfo::new(csid, bonsai))
    }

    async fn batch_derive_impl(
        ctx: &CoreContext,
        repo: &BlobRepo,
        csids: Vec<ChangesetId>,
        mapping: &BonsaiDerivedMappingContainer<Self>,
        _gap_size: Option<usize>,
    ) -> Result<HashMap<ChangesetId, Self>, Error> {
        // Derivation with gaps doesn't make much sense for changeset info, so
        // ignore the gap size.
        let cs_infos = stream::iter(csids.into_iter().map(|csid| async move {
            let bonsai = csid.load(ctx, repo.blobstore()).await?;
            let cs_info = ChangesetInfo::new(csid, bonsai);
            Ok::<_, Error>((csid, cs_info))
        }))
        .buffered(100)
        .try_collect::<HashMap<_, _>>()
        .await?;

        stream::iter(cs_infos.iter().map(Ok))
            .try_for_each_concurrent(100, |(csid, cs_info)| async move {
                mapping.put(ctx, *csid, cs_info).await
            })
            .await?;

        Ok(cs_infos)
    }
}

#[derive(Clone)]
pub struct ChangesetInfoMapping {
    blobstore: Arc<dyn Blobstore>,
}

#[async_trait]
impl BlobstoreRootIdMapping for ChangesetInfoMapping {
    type Value = ChangesetInfo;

    fn new(blobstore: Arc<dyn Blobstore>, _config: &DerivedDataTypesConfig) -> Result<Self> {
        Ok(Self { blobstore })
    }

    fn blobstore(&self) -> &dyn Blobstore {
        &self.blobstore
    }

    fn prefix(&self) -> &'static str {
        "changeset_info.blake2."
    }

    fn options(&self) {}
}

impl_bonsai_derived_mapping!(ChangesetInfoMapping, BlobstoreRootIdMapping, ChangesetInfo);

#[cfg(test)]
mod test {
    use super::*;

    use blobrepo_hg::BlobRepoHg;
    use blobstore::Loadable;
    use derived_data::BonsaiDerived;
    use fbinit::FacebookInit;
    use fixtures::linear;
    use futures::compat::Stream01CompatExt;
    use mercurial_types::HgChangesetId;
    use mononoke_types::BonsaiChangeset;
    use revset::AncestorsNodeStream;
    use std::collections::BTreeMap;
    use std::str::FromStr;
    use tests_utils::resolve_cs_id;

    #[fbinit::test]
    async fn derive_info_test(fb: FacebookInit) -> Result<(), Error> {
        let repo = linear::getrepo(fb).await;
        let ctx = CoreContext::test_mock(fb);

        let hg_cs_id = HgChangesetId::from_str("3c15267ebf11807f3d772eb891272b911ec68759").unwrap();
        let bcs_id = repo
            .get_bonsai_from_hg(ctx.clone(), hg_cs_id)
            .await?
            .unwrap();
        let bcs = bcs_id.load(&ctx, repo.blobstore()).await?;
        // Make sure that the changeset info was saved in the blobstore
        let info = ChangesetInfo::derive(&ctx, &repo, bcs_id).await?;

        check_info(&info, &bcs);
        Ok(())
    }

    fn check_info(info: &ChangesetInfo, bcs: &BonsaiChangeset) {
        assert_eq!(*info.changeset_id(), bcs.get_changeset_id());
        assert_eq!(info.message(), bcs.message());
        assert_eq!(
            info.parents().collect::<Vec<_>>(),
            bcs.parents().collect::<Vec<_>>()
        );
        assert_eq!(info.author(), bcs.author());
        assert_eq!(info.author_date(), bcs.author_date());
        assert_eq!(info.committer(), bcs.committer());
        assert_eq!(info.committer_date(), bcs.committer_date());
        assert_eq!(
            info.extra().collect::<BTreeMap<_, _>>(),
            bcs.extra().collect::<BTreeMap<_, _>>()
        );
    }

    #[fbinit::test]
    async fn batch_derive(fb: FacebookInit) -> Result<(), Error> {
        let ctx = CoreContext::test_mock(fb);
        let repo = linear::getrepo(fb).await;
        let master_cs_id = resolve_cs_id(&ctx, &repo, "master").await?;

        let mapping = BonsaiDerivedMappingContainer::new(
            ctx.fb,
            repo.name(),
            repo.get_derived_data_config().scuba_table.as_deref(),
            Arc::new(ChangesetInfo::default_mapping(&ctx, &repo)?),
        );
        let mut cs_ids =
            AncestorsNodeStream::new(ctx.clone(), &repo.get_changeset_fetcher(), master_cs_id)
                .compat()
                .try_collect::<Vec<_>>()
                .await?;
        cs_ids.reverse();
        let cs_infos =
            ChangesetInfo::batch_derive(&ctx, &repo, cs_ids.clone(), &mapping, None).await?;

        for cs_id in cs_ids {
            let bonsai = cs_id.load(&ctx, repo.blobstore()).await?;
            let cs_info = cs_infos
                .get(&cs_id)
                .expect("ChangesetInfo should have been derived");
            check_info(cs_info, &bonsai);
        }

        Ok(())
    }
}
