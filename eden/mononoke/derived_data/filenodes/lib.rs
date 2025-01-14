/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

#![deny(warnings)]

mod derive;
mod mapping;

pub use derive::generate_all_filenodes;
pub use mapping::{FilenodesOnlyPublic, FilenodesOnlyPublicMapping, PreparedRootFilenode};
