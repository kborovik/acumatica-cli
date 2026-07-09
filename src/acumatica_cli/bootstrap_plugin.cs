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
            "FinancialModule",
            "FinancialStandard",
            "DistributionModule",
            "Inventory",
            "Branch",
            "MultiCompany",
        };

        public override void UpdateDatabase()
        {
            var flags = new List<PXDataFieldAssign>();
            foreach (PropertyInfo prop in typeof(FeaturesSet).GetProperties())
            {
                if (prop.PropertyType == typeof(bool?))
                {
                    flags.Add(new PXDataFieldAssign(
                        prop.Name, PXDbType.Bit, Enabled.Contains(prop.Name)));
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
