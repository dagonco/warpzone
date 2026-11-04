import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightLinksValidator from 'starlight-links-validator';

export default defineConfig({
  site: 'https://dagonco.github.io',
  base: '/warpzone',
  integrations: [
    starlight({
      title: 'warpzone',
      description:
        'Keep one game library on a home server and deliver it to every console you own, with saves that follow you.',
      social: [
        {
          icon: 'github',
          label: 'GitHub',
          href: 'https://github.com/dagonco/warpzone',
        },
      ],
      editLink: {
        baseUrl: 'https://github.com/dagonco/warpzone/edit/main/website/',
      },
      plugins: [starlightLinksValidator()],
      sidebar: [
        { label: 'Home', slug: '' },
        {
          label: 'Overview',
          items: [
            { label: 'Quick start', slug: 'overview/quick-start' },
            { label: 'Installation', slug: 'overview/installation' },
            { label: 'Concepts: hub and devices', slug: 'overview/concepts' },
          ],
        },
        {
          label: 'Setup',
          items: [
            { label: 'Whitelists', slug: 'setup/whitelists' },
            { label: 'Adding games', slug: 'setup/inbox' },
            { label: 'BIOS', slug: 'setup/bios' },
            { label: 'Emulator settings', slug: 'setup/settings' },
          ],
        },
        {
          label: 'Console guides',
          items: [
            { label: 'Steam Deck / EmuDeck', slug: 'guides/emudeck' },
            { label: 'Anbernic', slug: 'guides/anbernic' },
            { label: 'Steam Machine', slug: 'guides/steam-machine' },
            { label: 'Adding a new console', slug: 'guides/new-console' },
          ],
        },
        {
          label: 'Operation',
          items: [
            { label: 'Updates', slug: 'operation/self-update' },
            { label: 'doctor', slug: 'operation/doctor' },
            { label: 'Safety', slug: 'operation/safety' },
          ],
        },
        {
          label: 'Reference',
          items: [
            { label: 'Commands', slug: 'reference/commands' },
            { label: 'Configuration (TOML)', slug: 'reference/configuration' },
          ],
        },
        { label: 'Troubleshooting', slug: 'troubleshooting' },
        { label: 'FAQ', slug: 'faq' },
        {
          label: 'Development',
          items: [
            { label: 'Design', slug: 'development/design' },
            {
              label: 'Contributing',
              link: 'https://github.com/dagonco/warpzone/blob/main/CONTRIBUTING.md',
              attrs: { target: '_blank' },
            },
            { label: 'Releasing', slug: 'development/releasing' },
            {
              label: 'Changelog',
              link: 'https://github.com/dagonco/warpzone/blob/main/CHANGELOG.md',
              attrs: { target: '_blank' },
            },
          ],
        },
      ],
    }),
  ],
});
