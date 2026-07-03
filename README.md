# AI-Generated Code Issues: A Taxonomy from Repository Histories, AI Conversations, and Developer Interviews

This repository contains the replication package for the for the study on AI-generated code issues

The package includes the scripts used to mine the analyzed instances and the data files resulting from the manual analysis.

The repository is organized into two main directories:

```
.
├── code/
└── data/
```

## Repository Structure

The repository is organized into two main folders:

```text
.
├── code/
└── data/
```

### `code/`

This folder contains the scripts used to collect and extract the data analyzed in the study.

- `minimg_commits.py`  
  Script used to mine the commits analyzed in the study.

- `mining_shared_link.py`  
  Script used to mine shared links to conversations between developers and ChatGPT that were posted by developers on GitHub.

- `mining_shared_link_in_files.py`  
  Script used to mine shared links to conversations between developers and ChatGPT that were included in files hosted on GitHub.

- `scrape_shared_conversations.py`  
  Script used to retrieve conversation data from the shared links obtained through the mining scripts.

### `data/`

This folder contains the data used and produced during the manual analysis.

- `true_postivie_instances.xlsx`  
  Excel file containing all valid instances identified during the manual analysis.  
  The file includes two sheets:
  - one sheet for commit-related instances;
  - one sheet for conversation-related instances.

- `is_true_positive_agreement_commits.csv`  
  CSV file containing the manual classifications assigned by the evaluators to the commit-related instances.  
  For each instance, the file reports whether the evaluators classified it as:
  - `true_positive`, meaning that the instance was considered relevant for the study;
  - `false_positive`, meaning that the instance was not considered relevant for the study.

- `is_true_positive_agreement_conversations.csv`  
  CSV file containing the manual classifications assigned by the evaluators to the conversation-related instances.  
  For each instance, the file reports whether the evaluators classified it as:
  - `true_positive`, meaning that the instance was considered relevant for the study;
  - `false_positive`, meaning that the instance was not considered relevant for the study.

The agreement CSV files were used to compute the agreement between evaluators during the manual analysis.
