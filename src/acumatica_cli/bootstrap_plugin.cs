// acu bootstrap CustomizationPlugin (SPEC T11).
//
// Runs in-process on customization publish - the one write path to
// FeaturesSet that works: the contract API cannot persist features no
// matter what endpoint fronts CS100000 (T3 verdict, docs/rest-api.md).
//
// Deliberately writes through PXDatabase, not FeaturesMaint: the graph
// save collides with the publish pipeline's concurrent plugin invocations
// ("Another process has added the 'FeaturesSet' record" - observed live,
// 26.101.0225) and nothing persists. A keyed row write is deterministic
// and idempotent: update the existing row, insert when absent. All 205
// NOT NULL bit columns are filled reflectively (only ~136 carry DB
// defaults); Status = 0 means Validated (PXIntList, verified vs live).
//
// The Enabled set is NOT authored here (SPEC V2: feature flags are config
// "what", never tool source - B6): package_zip() substitutes the
// ACU_FEATURES sentinel below with the data repo's bootstrap/features.yaml
// list (built-in six when the file is absent) at package-build time.
using System.Collections.Generic;
using System.Reflection;
using PX.Data;
using PX.Objects.CS;

namespace AcuBootstrap
{
    public class AcuBootstrapPlugin : Customization.CustomizationPlugin
    {
        private static readonly HashSet<string> Enabled = new HashSet<string>
        {
            /*ACU_FEATURES*/
        };

        public override void UpdateDatabase()
        {
            var flags = new List<PXDataFieldAssign>();
            var known = new HashSet<string>();
            foreach (PropertyInfo prop in typeof(FeaturesSet).GetProperties())
            {
                if (prop.PropertyType == typeof(bool?))
                {
                    known.Add(prop.Name);
                    flags.Add(new PXDataFieldAssign(
                        prop.Name, PXDbType.Bit, Enabled.Contains(prop.Name)));
                }
            }
            foreach (string name in Enabled)
            {
                if (!known.Contains(name))
                {
                    // silent-typo guard (T24): a misspelled features.yaml
                    // entry enables nothing - say so in the publish log
                    WriteLog("AcuBootstrap: unknown feature name '" + name
                        + "' - no FeaturesSet property, nothing enabled");
                }
            }
            flags.Add(new PXDataFieldAssign("Status", PXDbType.Int, 0)); // Validated

            using (var tx = new PXTransactionScope())
            {
                bool updated = PXDatabase.Update<FeaturesSet>(flags.ToArray());
                if (!updated)
                {
                    PXDatabase.Insert<FeaturesSet>(flags.ToArray());
                    WriteLog("AcuBootstrap: FeaturesSet row inserted (features enabled)");
                }
                else
                {
                    WriteLog("AcuBootstrap: FeaturesSet row updated (features enabled)");
                }
                tx.Complete();
            }
        }
    }
}
