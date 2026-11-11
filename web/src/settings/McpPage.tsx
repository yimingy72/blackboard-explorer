import PlatformCatalog from '../components/PlatformCatalog';
import { CatalogPage } from './SettingsShell';

export default function McpPage() {
  return <CatalogPage>{tracking => <PlatformCatalog {...tracking} />}</CatalogPage>;
}
