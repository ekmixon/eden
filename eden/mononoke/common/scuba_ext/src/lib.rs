/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

#![deny(warnings)]

use fbinit::FacebookInit;
use futures_ext::BoxFuture;
use futures_stats::{FutureStats, StreamStats};
use itertools::join;
pub use scuba::{ScubaSampleBuilder, ScubaValue};
use sshrelay::{Metadata, Preamble};
use std::convert::TryInto;
use time_ext::DurationExt;
use tracing::TraceContext;
use tunables::tunables;

#[cfg(fbcode_build)]
mod facebook;

pub use scribe_ext::ScribeClientImplementation;

pub trait ScubaSampleBuilderExt {
    fn with_opt_table(fb: FacebookInit, scuba_table: Option<String>) -> Self;
    fn add_preamble(&mut self, preamble: &Preamble) -> &mut Self;
    fn add_metadata(&mut self, metadata: &Metadata) -> &mut Self;
    fn log_with_msg<S: Into<Option<String>>>(&mut self, log_tag: &str, msg: S);
    fn add_stream_stats(&mut self, stats: &StreamStats) -> &mut Self;
    fn add_future_stats(&mut self, stats: &FutureStats) -> &mut Self;
    fn log_with_trace(&mut self, fb: FacebookInit, trace: &TraceContext) -> BoxFuture<(), ()>;
}

impl ScubaSampleBuilderExt for ScubaSampleBuilder {
    fn with_opt_table(fb: FacebookInit, scuba_table: Option<String>) -> Self {
        match scuba_table {
            None => ScubaSampleBuilder::with_discard(),
            Some(scuba_table) => ScubaSampleBuilder::new(fb, scuba_table),
        }
    }

    fn add_preamble(&mut self, preamble: &Preamble) -> &mut Self {
        self.add("repo", preamble.reponame.as_ref());
        for (key, value) in preamble.misc.iter() {
            self.add(key, value.as_ref());
        }
        self
    }

    fn add_metadata(&mut self, metadata: &Metadata) -> &mut Self {
        self.add("session_uuid", metadata.session_id().to_string());
        self.add("client_identities", join(metadata.identities().iter(), ","));

        if let Some(client_ip) = metadata.client_ip() {
            self.add("client_ip", client_ip.to_string());
        }
        if let Some(client_hostname) = metadata.client_hostname() {
            // "source_hostname" to remain compatible with historical logging
            self.add("source_hostname", client_hostname.to_owned());
        }
        if let Some(unix_name) = metadata.unix_name() {
            // "unix_username" to remain compatible with historical logging
            self.add("unix_username", unix_name);
        }

        self
    }

    fn log_with_msg<S: Into<Option<String>>>(&mut self, log_tag: &str, msg: S) {
        self.add("log_tag", log_tag);
        if let Some(mut msg) = msg.into() {
            match tunables().get_max_scuba_msg_length().try_into() {
                Ok(size) if size > 0 && msg.len() > size => {
                    msg.truncate(size);
                    msg.push_str(" (...)");
                }
                _ => {}
            };

            self.add("msg", msg);
        }
        self.log();
    }

    fn add_stream_stats(&mut self, stats: &StreamStats) -> &mut Self {
        self.add("poll_count", stats.poll_count)
            .add("poll_time_us", stats.poll_time.as_micros_unchecked())
            .add("count", stats.count)
            .add(
                "completion_time_us",
                stats.completion_time.as_micros_unchecked(),
            )
    }

    fn add_future_stats(&mut self, stats: &FutureStats) -> &mut Self {
        self.add("poll_count", stats.poll_count)
            .add("poll_time_us", stats.poll_time.as_micros_unchecked())
            .add(
                "completion_time_us",
                stats.completion_time.as_micros_unchecked(),
            )
    }

    fn log_with_trace(&mut self, fb: FacebookInit, trace: &TraceContext) -> BoxFuture<(), ()> {
        #[cfg(not(fbcode_build))]
        {
            use futures_ext::FutureExt;
            let _ = (fb, trace);
            futures::future::ok(()).boxify()
        }
        #[cfg(fbcode_build)]
        {
            facebook::log_with_trace(self, fb, trace)
        }
    }
}
