import PiModelCatalog from '../components/PiModelCatalog';
import { CatalogPage } from './SettingsShell';

export default function ModelsPage() {
  return <CatalogPage>{tracking => <PiModelCatalog {...tracking} />}</CatalogPage>;
}
