/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

#include "eden/fs/inodes/RequestContext.h"

#include <folly/logging/xlog.h>

#include "eden/fs/notifications/Notifications.h"
#include "eden/fs/telemetry/RequestMetricsScope.h"
#include "eden/fs/utils/SystemError.h"

using namespace std::chrono;

namespace facebook::eden {

void RequestContext::startRequest(
    EdenStats* stats,
    ChannelThreadStats::StatPtr stat,
    std::shared_ptr<RequestMetricsScope::LockedRequestWatchList>&
        requestWatches) {
  startTime_ = steady_clock::now();
  XDCHECK(latencyStat_ == nullptr);
  latencyStat_ = stat;
  stats_ = stats;
  channelThreadLocalStats_ = requestWatches;
  if (channelThreadLocalStats_) {
    requestMetricsScope_ = RequestMetricsScope(channelThreadLocalStats_.get());
  }
}

void RequestContext::finishRequest() {
  const auto now = steady_clock::now();

  const auto diff = now - startTime_;
  const auto diff_us = duration_cast<microseconds>(diff);
  const auto diff_ns = duration_cast<nanoseconds>(diff);

  stats_->getChannelStatsForCurrentThread().recordLatency(
      latencyStat_, diff_us);
  latencyStat_ = nullptr;
  stats_ = nullptr;

  if (channelThreadLocalStats_) {
    { auto temp = std::move(requestMetricsScope_); }
    channelThreadLocalStats_.reset();
  }

  if (auto pid = getClientPid(); pid.has_value()) {
    switch (getEdenTopStats().getFetchOrigin()) {
      case Origin::FromMemoryCache:
        pal_.recordAccess(
            *pid, ProcessAccessLog::AccessType::FsChannelMemoryCacheImport);
        break;
      case Origin::FromDiskCache:
        pal_.recordAccess(
            *pid, ProcessAccessLog::AccessType::FsChannelDiskCacheImport);
        break;
      case Origin::FromBackingStore:
        pal_.recordAccess(
            *pid, ProcessAccessLog::AccessType::FsChannelBackingStoreImport);
        break;
      default:
        break;
    }
    pal_.recordDuration(*pid, diff_ns);
  }
}

} // namespace facebook::eden
