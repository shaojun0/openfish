<script setup lang="ts">
import { ref } from 'vue'

import ArtifactCatalogView from '@/components/ArtifactCatalogView.vue'
import DebianOfflinePanel from '@/components/DebianOfflinePanel.vue'

/**
 * Remount token for the catalog: importing a bundle writes new `.deb` files
 * into the repository, and `/debian/Packages` is generated from disk, so the
 * list has to be re-read rather than patched.
 */
const catalogKey = ref(0)
</script>

<template>
  <div class="page">
    <ArtifactCatalogView :key="catalogKey" endpoint="debian" />
    <DebianOfflinePanel @imported="catalogKey += 1" />
  </div>
</template>
