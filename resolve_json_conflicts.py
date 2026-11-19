import json
import subprocess
import os

def merge_json(ours, theirs):
    if isinstance(ours, dict) and isinstance(theirs, dict):
        result = ours.copy()
        for key, value in theirs.items():
            if key in result:
                result[key] = merge_json(result[key], value)
            else:
                result[key] = value
        return result
    elif isinstance(ours, list) and isinstance(theirs, list):
        merged = ours + theirs
        unique = []
        seen = set()
        for item in merged:
            if isinstance(item, dict):
                identity = item.get('learning_id') or item.get('fingerprint') or item.get('id')
                if identity:
                    if identity not in seen:
                        unique.append(item)
                        seen.add(identity)
                    else:
                        # If seen, merge the objects
                        existing_idx = next(i for i, x in enumerate(unique) if (x.get('learning_id') or x.get('fingerprint') or x.get('id')) == identity)
                        unique[existing_idx] = merge_json(unique[existing_idx], item)
                else:
                    s = json.dumps(item, sort_keys=True)
                    if s not in seen:
                        unique.append(item)
                        seen.add(s)
            else:
                if item not in seen:
                    unique.append(item)
                    seen.add(item)
        return unique
    else:
        # Scalar conflict - pick theirs (remote) as it's usually newer for metadata
        return theirs

def resolve_file(file_path):
    try:
        # Get absolute path for git show
        cwd = os.getcwd()
        theirs_raw = subprocess.check_output(['git', 'show', ':2:' + file_path.replace('\\', '/')], encoding='utf-8')
        ours_raw = subprocess.check_output(['git', 'show', ':3:' + file_path.replace('\\', '/')], encoding='utf-8')
        
        theirs = json.loads(theirs_raw)
        ours = json.loads(ours_raw)
        
        merged = merge_json(ours, theirs)
        
        with open(file_path, 'w') as f:
            json.dump(merged, f, indent=2)
        
        subprocess.check_call(['git', 'add', file_path.replace('\\', '/')])
        print(f"Resolved {file_path}")
    except Exception as e:
        print(f"Failed to resolve {file_path}: {e}")

files = [
    "learning_engine_data/candidates/learned_recompute_findings.json",
    "learning_engine_data/candidates/learned_reconcile_waived_coverage_and_dependents.json",
    "learning_engine_data/candidates/learned_resolve_multi_column_gender.json",
    "learning_engine_data/candidates/pattern_census_cross_source_deduplication.json",
    "learning_engine_data/candidates/pattern_cross_source_dependent_collection_and_masked_override.json",
    "learning_engine_data/candidates/pattern_eligibility_and_dob_conflict_reconciliation.json",
    "learning_engine_data/candidates/pattern_hsa_medical_plan_classification.json",
    "learning_engine_data/candidates/pattern_match_percentage_and_workbook_formatting.json",
    "learning_engine_data/candidates/pattern_medical_only_disposition_and_status_precedence.json",
    "learning_engine_data/candidates/pattern_meritain_statement_parsing.json",
    "learning_engine_data/candidates/pattern_multi_column_gender_sex_resolution.json",
    "learning_engine_data/candidates/pattern_part_time_strict_exclusion.json",
    "learning_engine_data/candidates/pattern_post_sort_subscriber_row_recalculation.json",
    "learning_engine_data/candidates/pattern_preferred_name_and_middle_name_matching.json",
    "learning_engine_data/candidates/pattern_same_name_dob_disambiguation.json",
    "learning_engine_data/candidates/pattern_template_example_row_filtration.json",
    "learning_engine_data/registry/active_rules.json",
    "learning_engine_data/registry/index.json",
    "learning_engine_data/registry/validated_patterns.json",
    "learning_engine_data/rule_history/learned_recompute_findings/v1.json",
    "learning_engine_data/rule_history/learned_reconcile_waived_coverage_and_dependents/v1.json",
    "learning_engine_data/rule_history/learned_resolve_multi_column_gender/v1.json",
    "learning_engine_data/rule_history/pattern_census_cross_source_deduplication/v1.json",
    "learning_engine_data/rule_history/pattern_cross_source_dependent_collection_and_masked_override/v1.json",
    "learning_engine_data/rule_history/pattern_eligibility_and_dob_conflict_reconciliation/v1.json",
    "learning_engine_data/rule_history/pattern_hsa_medical_plan_classification/v1.json",
    "learning_engine_data/rule_history/pattern_match_percentage_and_workbook_formatting/v1.json",
    "learning_engine_data/rule_history/pattern_medical_only_disposition_and_status_precedence/v1.json",
    "learning_engine_data/rule_history/pattern_meritain_statement_parsing/v1.json",
    "learning_engine_data/rule_history/pattern_multi_column_gender_sex_resolution/v1.json",
    "learning_engine_data/rule_history/pattern_part_time_strict_exclusion/v1.json",
    "learning_engine_data/rule_history/pattern_post_sort_subscriber_row_recalculation/v1.json",
    "learning_engine_data/rule_history/pattern_preferred_name_and_middle_name_matching/v1.json",
    "learning_engine_data/rule_history/pattern_same_name_dob_disambiguation/v1.json",
    "learning_engine_data/rule_history/pattern_template_example_row_filtration/v1.json"
]

if __name__ == "__main__":
    # Automatically get all unmerged JSON files
    try:
        unmerged_files = subprocess.check_output(['git', 'diff', '--name-only', '--diff-filter=U'], encoding='utf-8').splitlines()
        json_files = [f for f in unmerged_files if f.endswith('.json')]
        
        for f in json_files:
            resolve_file(f)
            
        # For non-json files, just print them so we can handle manually if needed
        other_files = [f for f in unmerged_files if not f.endswith('.json')]
        if other_files:
            print(f"Manual attention needed for: {other_files}")
    except Exception as e:
        print(f"Error finding unmerged files: {e}")
