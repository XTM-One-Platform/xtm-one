# OpenCTI NLQ Filter Examples

Few-shot examples demonstrating correct OpenCTI filter generation from \
natural language queries. Each example shows a question and the expected \
JSON filter output.

---

Example 1
Question: Who's is behind this T1082?
Answer:
```json
{
  "filters": [
    {
      "key": "regardingOf",
      "values": [
        {
          "key": "id",
          "values": [
            "T1082"
          ]
        }
      ],
      "operator": "eq",
      "mode": "or"
    },
    {
      "key": "entity_type",
      "values": [
        "Threat-Actor-Group",
        "Threat-Actor-Individual",
        "Intrusion-Set"
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 2
Question: Who are the threats in the PolarEdge ORB report?
Answer:
```json
{
  "filters": [
    {
      "key": "regardingOf",
      "values": [
        {
          "key": "id",
          "values": [
            "PolarEdge ORB report"
          ]
        }
      ],
      "operator": "eq",
      "mode": "or"
    },
    {
      "key": "entity_type",
      "values": [
        "Intrusion-Set",
        "Threat-Actor-Group",
        "Threat-Actor-Individual"
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 3
Question: Which risks are most likely to affect me?
Answer:
```json
{
  "filters": [
    {
      "key": "regardingOf",
      "values": [
        {
          "key": "relationship_type",
          "values": [
            "targets"
          ]
        }
      ],
      "operator": "eq",
      "mode": "or"
    },
    {
      "key": "entity_type",
      "values": [
        "Threat-Actor-Group",
        "Threat-Actor-Individual",
        "Intrusion-Set"
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 4
Question: Which victims and industry sectors are being affected by 134.175.104.84?
Answer:
```json
{
  "filters": [
    {
      "key": "regardingOf",
      "values": [
        {
          "key": "relationship_type",
          "values": [
            "targets"
          ]
        },
        {
          "key": "id",
          "values": [
            "134.175.104.84"
          ]
        }
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 5
Question: How would Cyber Av3ngers carry out an attack on me?
Answer:
```json
{
  "filters": [
    {
      "key": "regardingOf",
      "values": [
        {
          "key": "id",
          "values": [
            "Cyber Av3ngers"
          ]
        },
        {
          "key": "relationship_type",
          "values": [
            "uses"
          ]
        }
      ],
      "operator": "eq",
      "mode": "or"
    },
    {
      "key": "entity_type",
      "values": [
        "Attack-Pattern"
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 6
Question: Can you list the malware used by MustardMan?
Answer:
```json
{
  "filters": [
    {
      "key": "regardingOf",
      "values": [
        {
          "key": "id",
          "values": [
            "MustardMan"
          ]
        },
        {
          "key": "relationship_type",
          "values": [
            "uses"
          ]
        }
      ],
      "operator": "eq",
      "mode": "or"
    },
    {
      "key": "entity_type",
      "values": [
        "Malware"
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 7
Question: Can you list the IOCs linked to APT-C-00 Ocean Lotus?
Answer:
```json
{
  "filters": [
    {
      "key": "regardingOf",
      "values": [
        {
          "key": "id",
          "values": [
            "APT-C-00  Ocean Lotus"
          ]
        }
      ],
      "operator": "eq",
      "mode": "or"
    },
    {
      "key": "entity_type",
      "values": [
        "Indicator"
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 8
Question: Does the file named 'example_file' have any associations with known threat actors or cyber threats?
Answer:
```json
{
  "filters": [
    {
      "key": "entity_type",
      "values": [
        "Threat-Actor-Group",
        "Threat-Actor-Individual",
        "Intrusion-Set"
      ],
      "operator": "eq",
      "mode": "or"
    },
    {
      "key": "regardingOf",
      "values": [
        {
          "key": "relationship_type",
          "values": [
            "related-to"
          ]
        },
        {
          "key": "id",
          "values": [
            "example_file"
          ]
        }
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 9
Question: What intelligence reports have been released by the Cambridge Group of Clubs?
Answer:
```json
{
  "filters": [
    {
      "key": "creator_id",
      "values": [
        "Cambridge Group of Clubs"
      ],
      "operator": "eq",
      "mode": "or"
    },
    {
      "key": "entity_type",
      "values": [
        "Report"
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 10
Question: Can you list all cybersecurity incidents assigned to John Doe?
Answer:
```json
{
  "filters": [
    {
      "key": "entity_type",
      "values": [
        "Incident"
      ],
      "operator": "eq",
      "mode": "or"
    },
    {
      "key": "objectAssignee",
      "values": [
        "John Doe"
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 11
Question: What vulnerabilities are associated with T1497?
Answer:
```json
{
  "filters": [
    {
      "key": "regardingOf",
      "values": [
        {
          "key": "id",
          "values": [
            "T1497"
          ]
        }
      ],
      "operator": "eq",
      "mode": "or"
    },
    {
      "key": "entity_type",
      "values": [
        "Vulnerability"
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 12
Question: Which victims and industry sectors are targeted by 134.175.104.84?
Answer:
```json
{
  "filters": [
    {
      "key": "regardingOf",
      "values": [
        {
          "key": "relationship_type",
          "values": [
            "targets"
          ]
        },
        {
          "key": "id",
          "values": [
            "134.175.104.84"
          ]
        }
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 13
Question: Which actors are located in Russia?
Answer:
```json
{
  "filters": [
    {
      "key": "regardingOf",
      "values": [
        {
          "key": "relationship_type",
          "values": [
            "located-at"
          ]
        },
        {
          "key": "id",
          "values": [
            "Russia"
          ]
        }
      ],
      "operator": "eq",
      "mode": "or"
    },
    {
      "key": "entity_type",
      "values": [
        "Threat-Actor-Group",
        "Threat-Actor-Individual",
        "Intrusion-Set"
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 14
Question: What is the impact of quantum computing on encryption?
Answer:
```json
{
  "filters": [],
  "mode": "and"
}
```

Example 15
Question: The sun, a radiant beacon in the sky, spread its golden warmth across the horizon, igniting the dawn with an explosion of brilliant color.
Answer:
```json
{
  "filters": [],
  "mode": "and"
}
```

Example 16
Question: If T1497 was involved, who would be responsible?
Answer:
```json
{
  "filters": [
    {
      "key": "regardingOf",
      "values": [
        {
          "key": "id",
          "values": [
            "T1497"
          ]
        }
      ],
      "operator": "eq",
      "mode": "or"
    },
    {
      "key": "entity_type",
      "values": [
        "Threat-Actor-Group",
        "Threat-Actor-Individual",
        "Intrusion-Set"
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 17
Question: I wonder who is behind T1497.
Answer:
```json
{
  "filters": [
    {
      "key": "regardingOf",
      "values": [
        {
          "key": "id",
          "values": [
            "T1497"
          ]
        }
      ],
      "operator": "eq",
      "mode": "or"
    },
    {
      "key": "entity_type",
      "values": [
        "Threat-Actor-Group",
        "Threat-Actor-Individual",
        "Intrusion-Set"
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 18
Question: Who uses either T1497 or T1082?
Answer:
```json
{
  "filters": [
    {
      "key": "regardingOf",
      "values": [
        {
          "key": "id",
          "values": [
            "T1497"
          ]
        },
        {
          "key": "id",
          "values": [
            "T1082"
          ]
        },
        {
          "key": "relationship_type",
          "values": [
            "uses"
          ]
        }
      ],
      "operator": "eq",
      "mode": "or"
    },
    {
      "key": "entity_type",
      "values": [
        "Threat-Actor-Group",
        "Threat-Actor-Individual",
        "Intrusion-Set"
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 19
Question: Show me vulnerabilities with a CVSS score > 3.5 and <= to 7.
Answer:
```json
{
  "filters": [
    {
      "key": "x_opencti_cvss_base_score",
      "values": [
        "3.5"
      ],
      "operator": "gt",
      "mode": "or"
    },
    {
      "key": "x_opencti_cvss_base_score",
      "values": [
        "7"
      ],
      "operator": "lte",
      "mode": "or"
    },
    {
      "key": "entity_type",
      "values": [
        "Vulnerability"
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 20
Question: Retrieve all entities tagged with label 'apt50'.
Answer:
```json
{
  "filters": [
    {
      "key": "objectLabel",
      "values": [
        "apt50"
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 21
Question: Find all geographical cities.
Answer:
```json
{
  "filters": [
    {
      "key": "entity_type",
      "values": [
        "City"
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 22
Question: Who are the victims of APT28 in Europe?
Answer:
```json
{
  "filters": [
    {
      "key": "regardingOf",
      "values": [
        {
          "key": "relationship_type",
          "values": [
            "targets"
          ]
        },
        {
          "key": "id",
          "values": [
            "APT28"
          ]
        }
      ],
      "operator": "eq",
      "mode": "or"
    },
    {
      "key": "regardingOf",
      "values": [
        {
          "key": "relationship_type",
          "values": [
            "located-at"
          ]
        },
        {
          "key": "id",
          "values": [
            "Europe"
          ]
        }
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```

Example 23
Question: Find all incidents linked to APT28.
Answer:
```json
{
  "filters": [
    {
      "key": "regardingOf",
      "values": [
        {
          "key": "id",
          "values": [
            "APT28"
          ]
        }
      ],
      "operator": "eq",
      "mode": "or"
    },
    {
      "key": "entity_type",
      "values": [
        "Incident"
      ],
      "operator": "eq",
      "mode": "or"
    }
  ],
  "mode": "and"
}
```
