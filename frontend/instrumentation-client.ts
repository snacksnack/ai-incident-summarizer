import { datadogRum } from '@datadog/browser-rum'

// RC1-344: RUM + Session Replay on real visitors, mirroring the portfolio
// (RC1-343). Next runs this file client-side before the app hydrates, so no
// component wiring is needed. Prod-only so dev sessions never pollute the
// data; the client token is RUM's public browser token — it ships in the
// bundle by design.
if (process.env.NODE_ENV === 'production') {
  datadogRum.init({
    applicationId: '3001bfdf-1f05-440d-bae0-d31948e5dce9',
    clientToken: 'pubb9c4710a9ae37009d0b3fa1bfd2ebae3',
    site: 'datadoghq.com',
    service: 'incidents-hihelloreid',
    env: 'prod',
    sessionSampleRate: 100,
    sessionReplaySampleRate: 100,
    defaultPrivacyLevel: 'mask-user-input',
    trackUserInteractions: true,
    trackResources: true,
    trackLongTasks: true,
  })
}
