/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

use std::collections::{hash_map::Entry, HashMap};
use std::convert::TryInto;
use std::path::Path;
use std::sync::Arc;

use anyhow::{anyhow, Result};
use log::warn;
use tracing::{event, instrument, Level};

use configparser::config::ConfigSet;
use manifest::List;
use manifest_tree::TreeEntry;
use progress::null::NullProgressFactory;
use revisionstore::{
    scmstore::{FileAttributes, FileStore, FileStoreBuilder, TreeStore, TreeStoreBuilder},
    HgIdDataStore, MemcacheStore,
};
use types::{HgId, Key, RepoPathBuf};

use crate::utils::key_from_path_node_slice;

pub struct BackingScmStores {
    filestore: Arc<FileStore>,
    treestore: Arc<TreeStore>,
}

impl BackingScmStores {
    pub fn new(config: &ConfigSet, hg: impl AsRef<Path>, use_edenapi: bool) -> Result<Self> {
        let store_path = hg.as_ref().join("store");

        let mut filestore = FileStoreBuilder::new(&config)
            .local_path(&store_path)
            .override_edenapi(use_edenapi);

        let treestore = TreeStoreBuilder::new(&config)
            .override_edenapi(use_edenapi)
            .local_path(&store_path)
            .suffix(Path::new("manifests"));

        // Memcache takes 30s to initialize on debug builds slowing down tests significantly, let's
        // not even try to initialize it then.
        if !cfg!(debug_assertions) {
            match MemcacheStore::new(&config, NullProgressFactory::arc()) {
                Ok(memcache) => {
                    // XXX: Add the memcachestore for the treestore.
                    filestore = filestore.memcache(Arc::new(memcache));
                }
                Err(e) => warn!("couldn't initialize Memcache: {}", e),
            }
        }

        Ok(Self {
            filestore: Arc::new(filestore.build()?),
            treestore: Arc::new(treestore.build()?),
        })
    }

    /// Reads file from blobstores. When `local_only` is true, this function will only read blobs
    /// from on disk stores.
    pub fn get_blob(&self, path: &[u8], node: &[u8], local_only: bool) -> Result<Option<Vec<u8>>> {
        let key = key_from_path_node_slice(path, node)?;
        self.get_blob_by_key(key, local_only)
    }

    #[instrument(level = "debug", skip(self))]
    fn get_blob_by_key(&self, key: Key, local_only: bool) -> Result<Option<Vec<u8>>> {
        let local = self.filestore.local();
        let fetch_result = if local_only {
            event!(Level::TRACE, "attempting to fetch blob locally");
            &local
        } else {
            self.filestore.as_ref()
        }
        .fetch(std::iter::once(key), FileAttributes::CONTENT)
        .single();

        Ok(if let Some(mut file) = fetch_result? {
            Some(file.file_content()?.into_vec())
        } else {
            None
        })
    }

    /// Fetch file contents in batch. Whenever a blob is fetched, the supplied `resolve` function is
    /// called with the file content or an error message, and the index of the blob in the request
    /// array. When `local_only` is enabled, this function will only check local disk for the file
    /// content.
    #[instrument(level = "debug", skip(self, resolve))]
    pub fn get_blob_batch<F>(&self, keys: Vec<Result<Key>>, local_only: bool, resolve: F)
    where
        F: Fn(usize, Result<Option<Vec<u8>>>) -> (),
    {
        // Resolve key errors
        let requests = keys
            .into_iter()
            .enumerate()
            .filter_map(|(index, key)| match key {
                Ok(key) => Some((index, key)),
                Err(e) => {
                    // return early when the key is invalid
                    resolve(index, Err(e));
                    None
                }
            });

        // Crate key-index mapping and fail fast for duplicate keys
        let mut indexes: HashMap<Key, usize> = HashMap::new();
        for (index, key) in requests {
            if let Entry::Vacant(vacant) = indexes.entry(key) {
                vacant.insert(index);
            } else {
                resolve(
                    index,
                    Err(anyhow!(
                        "duplicated keys are not supported by get_blob_batch when using scmstore",
                    )),
                );
            }
        }

        // Handle local-only fetching
        let local = self.filestore.local();
        let fetch_results = if local_only {
            event!(Level::TRACE, "attempting to fetch blobs locally");
            &local
        } else {
            self.filestore.as_ref()
        }
        .fetch(indexes.keys().cloned(), FileAttributes::CONTENT)
        .fetch_results();

        // Handle fetch results
        for (key, res) in fetch_results {
            let res = res.and_then(|opt| {
                opt.map(|mut file| file.file_content().map(|content| content.into_vec()))
                    .transpose()
            });
            if let Some(index) = indexes.remove(&key) {
                resolve(index, res)
            } else {
                tracing::error!(
                    "no index found for {}, scmstore returned a key we have no record of requesting",
                    key
                );
            }
        }
    }

    #[instrument(level = "debug", skip(self))]
    pub fn get_tree(&self, node: &[u8], local_only: bool) -> Result<Option<List>> {
        let hgid = HgId::from_slice(node)?;
        let key = Key::new(RepoPathBuf::new(), hgid);

        let local = self.treestore.local();
        let mut fetch_results = if local_only {
            event!(Level::TRACE, "attempting to fetch trees locally");
            &local
        } else {
            self.treestore.as_ref()
        }
        .fetch_batch(std::iter::once(key.clone()))?;

        // TODO(meyer): Standardize error handling across trees and files, support Option not found
        if let Some(mut entry) = fetch_results.complete.pop() {
            Ok(Some(
                entry
                    .content()
                    // TODO(meyer): Make manifest-tree crate use minibytes::Bytes
                    .map(|c| c.into_vec().into())
                    .map(TreeEntry)
                    .and_then(|entry| entry.try_into())?,
            ))
        } else {
            Ok(None)
        }
    }

    /// Fetch tree contents in batch. Whenever a tree is fetched, the supplied `resolve` function is
    /// called with the tree content or an error message, and the index of the tree in the request
    /// array. When `local_only` is enabled, this function will only check local disk for the file
    /// content.
    #[instrument(level = "debug", skip(self, resolve))]
    pub fn get_tree_batch<F>(&self, keys: Vec<Result<Key>>, local_only: bool, resolve: F)
    where
        F: Fn(usize, Result<Option<List>>) -> (),
    {
        // Handle key errors
        let requests = keys
            .into_iter()
            .enumerate()
            .filter_map(|(index, key)| match key {
                Ok(key) => Some((index, key)),
                Err(e) => {
                    // return early when the key is invalid
                    resolve(index, Err(e));
                    None
                }
            });

        // Crate key-index mapping and fail fast for duplicate keys
        let mut indexes: HashMap<Key, usize> = HashMap::new();
        for (index, key) in requests {
            if let Entry::Vacant(vacant) = indexes.entry(key) {
                vacant.insert(index);
            } else {
                resolve(
                    index,
                    Err(anyhow!(
                        "duplicated keys are not supported by get_tree_batch when using scmstore",
                    )),
                );
            }
        }

        // Handle local-only fetching
        let local = self.treestore.local();
        let fetch_results = if local_only {
            event!(Level::TRACE, "attempting to fetch trees locally");
            &local
        } else {
            self.treestore.as_ref()
        }
        .fetch_batch(indexes.keys().cloned());

        // Handle batch failure
        let fetch_results = match fetch_results {
            Ok(res) => res,
            Err(e) => {
                let mut indexes = indexes.values();
                // Pass along the error to the first index
                if let Some(index) = indexes.next() {
                    resolve(*index, Err(e))
                }
                // Return a generic error for others (errors are not Clone)
                for index in indexes {
                    resolve(
                        *index,
                        Err(anyhow!("get_tree_batch failed across the entire batch")),
                    )
                }
                return;
            }
        };

        // Handle pey-key fetch results
        for mut entry in fetch_results.complete {
            let key = entry.key().clone();
            let res = entry
                .content()
                // TODO(meyer): Make manifest-tree crate use minibytes::Bytes
                .map(|c| c.into_vec().into())
                .map(TreeEntry)
                .and_then(|entry| entry.try_into())
                .map(Some);
            if let Some(index) = indexes.remove(&key) {
                resolve(index, res)
            } else {
                tracing::error!(
                    "no index found for {}, scmstore returned a key we have no record of requesting",
                    key
                );
            }
        }

        for key in fetch_results.incomplete {
            if let Some(index) = indexes.remove(&key) {
                // TODO(meyer): Standardize error handling across trees and files, support Option not found
                resolve(index, Ok(None))
            } else {
                tracing::error!(
                    "no index found for {}, scmstore returned a key we have no record of requesting",
                    key
                );
            }
        }
    }

    /// Forces backing store to rescan pack files or local indexes
    #[instrument(level = "debug", skip(self))]
    pub fn refresh(&self) {
        self.filestore.refresh().ok();
        self.treestore.refresh().ok();
    }
}
