// SPDX-License-Identifier: Apache-2.0

/**
 * The Filigran AI Gateway credential, `<url>[||<target>[||<token>]]`, as the
 * backend gateway provider parses it. The form sets no target, hence the empty
 * segment before a token.
 */
export function buildGatewayCredential(url: string, token: string): string {
  const base = url.trim();
  const secret = token.trim();
  return secret ? `${base}||||${secret}` : base;
}
